import os, sys, tempfile, time, dataclasses
sys.path.insert(0, os.getcwd())
tmp = tempfile.mkdtemp()
import chromadb
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import retriever, global_settings, config, alerts
from app.admin import api as A, users, kbs_api, documents_api
from app.admin.auth import SessionSigner
from app.channels.manager import manager
from app.channels.store import ChannelStore
from app.db import Database
from app.knowledge import kbs, documents, versions
from app.secretstore import FernetSecretStore, generate_key

db = Database(os.path.join(tmp, "t.db")); db.migrate()
client = chromadb.PersistentClient(path=os.path.join(tmp, "chroma"))
retriever.get_client = lambda: client
kbs.get_db = documents.get_db = global_settings.get_db = versions.get_db = lambda: db
cfg = dataclasses.replace(config.settings, docs_dir=os.path.join(tmp, "files"), max_upload_mb=5)
documents.settings = documents_api.settings = cfg
documents.RETRY_WAIT = (0.01, 0.05)
for _m in list(sys.modules.values()):                     # every module that reads the database reads this one, whatever it imported
    if getattr(_m, "__name__", "").startswith("app") and hasattr(_m, "get_db"):
        _m.get_db = lambda: db
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)

default = kbs.ensure_default(db)
col = retriever.get_collection()
col.upsert(ids=["HR-Policy.pdf::0"], documents=["Earned leave is 18 days."], metadatas=[{"source": "HR-Policy.pdf", "doc_id": "HR-Policy.pdf", "page": 1}])
kbs.backfill_metadata(db, default)
documents.import_existing(db)
versions.ensure_versions(db)
v1d = versions.active(db, default["id"])
ok(v1d["version"] == 1 and v1d["collection"] == "docs", "existing index became version 1 of the default KB")

store = ChannelStore(db, FernetSecretStore(generate_key()), lambda t: manager.adapter_class(t).SPEC)
ctx = A.AdminContext(db=db, store=store, manager=manager, signer=SessionSigner(generate_key()), domain="sysfore.com", allow_remote=True)
users.create_admin(db, "boss@sysfore.com", "correct horse battery", domain="sysfore.com")
app = FastAPI()
for r in (A.router, kbs_api.router, documents_api.router):
    app.include_router(r)
app.dependency_overrides[A.get_ctx] = lambda: ctx
c = TestClient(app, base_url="http://localhost")
r = c.post("/admin/api/auth/login", json={"email": "boss@sysfore.com", "password": "correct horse battery"})
h = {"X-CSRF-Token": r.json()["csrf"]}
it = c.post("/admin/api/kbs", headers=h, json={"name": "IT help", "department_id": default["department_id"]}).json()
ok(versions.active(db, it["id"])["version"] == 1, "a new KB starts with version 1")


def wait_doc(doc_id, t=60):
    end = time.time() + t
    while time.time() < end:
        d = documents.get(db, doc_id)
        if d["status"] in ("Indexed", "Failed"):
            return d
        time.sleep(0.2)
    return documents.get(db, doc_id)


def wait_build(kb_id, t=90):
    end = time.time() + t
    while time.time() < end:
        if not versions.rebuilding(db, kb_id):
            return
        time.sleep(0.2)


def up(kb, name, data):
    r = c.post(f"/admin/api/kbs/{kb}/documents", headers=h, files={"file": (name, data)})
    if r.status_code == 201:
        wait_doc(r.json()["id"])
    return r


text = "\n\n".join(f"Section {i}. The VPN token for staff number {i} is reset by the service desk after identity checks. " * 4 for i in range(12))
a = up(it["id"], "vpn.txt", text.encode()); b = up(it["id"], "laptops.txt", ("Laptop refresh every three years. " * 60).encode())
n_before = versions.active(db, it["id"])["chunks"]
vpn_chunks = documents.get(db, a.json()["id"])["chunks"]
ok(not versions.out_of_date(db, kbs.get_kb(db, it["id"])), "index matches the settings after upload")

# changing chunk settings does NOT touch the active index
r = c.put(f"/admin/api/kbs/{it['id']}", headers=h, json={"chunk_size": 300, "chunk_overlap": 50})
ok(r.status_code == 200 and "rebuild" in r.json().get("note", "").lower(), "settings saved with a 'rebuild to apply' note")
ok(versions.out_of_date(db, kbs.get_kb(db, it["id"])), "index is now flagged out of date")
ok(retriever.retrieve_scored("VPN token reset", kb_ids=[it["id"]])[0]["source"] == "vpn.txt", "the old index keeps answering")
c.post(f"/admin/api/documents/{a.json()['id']}/reindex", headers=h); d = wait_doc(a.json()["id"])
ok(d["chunks"] == vpn_chunks, "single-document re-index uses the ACTIVE index settings (strategies not mixed)")

# estimate
e = c.get(f"/admin/api/kbs/{it['id']}/index/estimate", headers=h).json()
ok(e["documents"] == 2 and e["chunks"] > n_before and e["cost_usd"] == 0.0 and e["seconds"] is not None and e["target"]["chunk_size"] == 300, f"estimate: {e['chunks']} chunks, {e['seconds']} s, cost {e['cost_usd']}")

# rebuild with a slow store so we can observe the 'old index serves' window
real_add = kbs.ChromaStore.add
slow = {"on": True}


def slow_add(self, ids, texts, metas):
    if slow["on"]:
        time.sleep(0.5)
    return real_add(self, ids, texts, metas)


kbs.ChromaStore.add = slow_add
r = c.post(f"/admin/api/kbs/{it['id']}/index/rebuild", headers=h)
ok(r.status_code == 202 and r.json()["version"] == 2 and r.json()["collection"] == f"kb_{it['id']}_v2", "rebuild started as version 2 in a new collection")
time.sleep(0.4)
ok(versions.active(db, it["id"])["version"] == 1 and retriever.retrieve_scored("VPN token reset", kb_ids=[it["id"]])[0]["source"] == "vpn.txt", "during the build, version 1 is still active and answering")
ok(c.post(f"/admin/api/kbs/{it['id']}/documents", headers=h, files={"file": ("late.txt", b"hello")}).status_code == 409, "uploads are refused (409) while it builds")
ok(c.post(f"/admin/api/kbs/{it['id']}/index/rebuild", headers=h).status_code == 409, "a second rebuild is refused")
wait_build(it["id"])
act = versions.active(db, it["id"])
ok(act["version"] == 2 and act["chunk_size"] == 300 and act["status"] == "Ready", "switched to version 2 only after it completed")
ok(kbs.get_kb(db, it["id"])["collection"] == f"kb_{it['id']}_v2" and not versions.out_of_date(db, kbs.get_kb(db, it["id"])), "KB now points at the new collection; no longer out of date")
ok(act["chunks"] > n_before and documents.get(db, a.json()["id"])["chunks"] > vpn_chunks, "new chunk size gives more chunks; per-document counts updated")
ok(retriever.retrieve_scored("VPN token reset", kb_ids=[it["id"]])[0]["source"] == "vpn.txt", "answers come from the new index")
vs = {v["version"]: v for v in versions.list_versions(db, it["id"])}
ok(vs[1]["status"] == "Ready" and not vs[1]["is_active"] and f"kb_{it['id']}" in [x.name for x in client.list_collections()], "version 1 is kept for rollback")
slow["on"] = False

# documents changed after the switch, then roll back
late = up(it["id"], "late.txt", b"Parking is free on Fridays for all staff members.")
gone = c.delete(f"/admin/api/documents/{b.json()['id']}", headers=h)
ok(late.status_code == 201 and gone.status_code == 200, "a document added and one deleted after the switch")
r = c.post(f"/admin/api/kbs/{it['id']}/index/rollback", headers=h, json={"version_id": vs[1]["id"]})
ok(r.status_code == 200 and versions.active(db, it["id"])["version"] == 1, "rolled back to version 1")
time.sleep(0.5)
wait_doc(late.json()["id"])
ok(retriever.retrieve_scored("parking free fridays", kb_ids=[it["id"]])[0]["source"] == "late.txt", "document added after the switch was brought into the old index")
ok(not [x for x in retriever.retrieve_scored("laptop refresh three years", kb_ids=[it["id"]]) if x["source"] == "laptops.txt"], "document deleted after the switch is gone from the old index")
ok(documents.get(db, a.json()["id"])["chunks"] == vpn_chunks, "per-document chunk counts match the restored index")

# keep only the last two: build v3 now
c.put(f"/admin/api/kbs/{it['id']}", headers=h, json={"chunk_size": 500, "chunk_overlap": 50})
c.post(f"/admin/api/kbs/{it['id']}/index/rebuild", headers=h); wait_build(it["id"])
vs = versions.list_versions(db, it["id"])
ready = sorted(v["version"] for v in vs if v["status"] == "Ready")
ok(versions.active(db, it["id"])["version"] == 3 and len(ready) == 2 and 1 not in ready, f"after v3 only two versions are kept: {ready}")
ok(f"kb_{it['id']}" not in [x.name for x in client.list_collections()], "the oldest version's collection was deleted")

# failure: active untouched, nothing left behind, alert raised
kbs.ChromaStore.add = lambda self, ids, texts, metas: (_ for _ in ()).throw(RuntimeError("embedding service unavailable"))
c.put(f"/admin/api/kbs/{it['id']}", headers=h, json={"chunk_size": 400, "chunk_overlap": 40})
c.post(f"/admin/api/kbs/{it['id']}/index/rebuild", headers=h); wait_build(it["id"])
failed = versions.list_versions(db, it["id"])[0]
ok(failed["status"] == "Failed" and "unavailable" in failed["error"] and versions.active(db, it["id"])["version"] == 3, "failed rebuild: marked Failed, version 3 still active")
ok(failed["collection"] not in [x.name for x in client.list_collections()], "failed rebuild left no half-built collection")
ok(any(a_["kind"] == "index_build_failed" for a_ in alerts.active(db)), "admin alert raised")

# cancel
kbs.ChromaStore.add = slow_add; slow["on"] = True
c.post(f"/admin/api/kbs/{it['id']}/index/rebuild", headers=h); time.sleep(0.4)
ok(c.post(f"/admin/api/kbs/{it['id']}/index/cancel", headers=h).status_code == 200, "cancel accepted")
wait_build(it["id"])
cancelled = versions.list_versions(db, it["id"])[0]
ok(cancelled["status"] == "Cancelled" and versions.active(db, it["id"])["version"] == 3, "cancelled build leaves the active index alone")
slow["on"] = False; kbs.ChromaStore.add = real_add

# restart recovery
with db.session() as s:
    from app.db import KBIndexVersion
    s.add(KBIndexVersion(kb_id=it["id"], version=99, collection="kb_x_v99", chunk_size=300, chunk_overlap=50, status="Building"))
ok(versions.recover_interrupted(db) == 1 and not versions.rebuilding(db, it["id"]), "a build interrupted by a restart is marked failed")

# deleting the KB removes every version
cols_before = [x.name for x in client.list_collections() if x.name.startswith(f"kb_{it['id']}")]
r = c.delete(f"/admin/api/kbs/{it['id']}", headers=h)
ok(r.status_code == 200 and not [x.name for x in client.list_collections() if x.name.startswith(f"kb_{it['id']}")] and versions.list_versions(db, it["id"]) == [],
   f"deleting the KB removed all version collections ({len(cols_before)}) and records")
ok(retriever.retrieve_scored("earned leave")[0]["source"] == "HR-Policy.pdf", "default knowledge base unaffected")
