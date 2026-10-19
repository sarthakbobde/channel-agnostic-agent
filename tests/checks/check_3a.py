import os, sys, tempfile, dataclasses
sys.path.insert(0, os.getcwd())
tmp = tempfile.mkdtemp()
import chromadb
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import retriever, ingest, global_settings
from app.admin import api as A, users, kbs_api
from app.admin.auth import SessionSigner
from app.channels.manager import manager
from app.channels.store import ChannelStore
from app.db import Database
from app.knowledge import kbs, metadata
from app.secretstore import FernetSecretStore, generate_key

db = Database(os.path.join(tmp, "t.db")); db.migrate()
client = chromadb.PersistentClient(path=os.path.join(tmp, "chroma"))
retriever.get_client = lambda: client
kbs.get_db = lambda: db
global_settings.get_db = lambda: db
for _m in list(sys.modules.values()):                     # every module that reads the database reads this one, whatever it imported
    if getattr(_m, "__name__", "").startswith("app") and hasattr(_m, "get_db"):
        _m.get_db = lambda: db
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)

# 1. legacy chunks (only source / doc_id / page) get the full schema; citations unchanged
col = retriever.get_collection()
col.upsert(ids=["HR-Policy.pdf::0", "HR-Policy.pdf::1"], documents=["Earned leave is 18 days per year.", "Sick leave is 12 days."],
           metadatas=[{"source": "HR-Policy.pdf", "doc_id": "HR-Policy.pdf", "page": 3}, {"source": "HR-Policy.pdf", "doc_id": "HR-Policy.pdf", "page": 4}])
default = kbs.ensure_default(db)
ok(default["is_default"] and default["collection"] == "docs", "default KB points at the existing collection")
n = kbs.backfill_metadata(db, default)
metas = [m for _, ms in kbs.store_for(default).iter_metadata() for m in ms]
ok(n == 2 and all(not metadata.missing(m) for m in metas), "legacy chunks now carry the full metadata schema")
ok(kbs.backfill_metadata(db, default) == 0, "backfill is idempotent")
from app.db import Document          # a chunk is only returned if it belongs to a known document: register the legacy file like the importer does
with db.session() as _s:
    _s.add(Document(kb_id=default["id"], name="HR-Policy.pdf", doc_key="HR-Policy.pdf", status="Indexed", chunks=2))
hit = retriever.retrieve_scored("how many earned leave days")[0]
ok(hit["source"] == "HR-Policy.pdf" and hit["page"] in (3, 4), "citation (file + page) still works")

# 2. a second KB with its own collection, ingest, and retrieval scoped to it
dept = kbs.department_name(db, default["department_id"])
second = kbs.create_kb(db, "IT help", "Laptops and VPN", default["department_id"], chunk_size=300, chunk_overlap=50)
ok(second["collection"] == f"kb_{second['id']}", "second KB gets its own collection")
d = os.path.join(tmp, "docs"); os.makedirs(d)
open(os.path.join(d, "vpn.txt"), "w").write("To reset your VPN token, raise a ticket with the IT service desk. " * 20)
ingest.ingest(d, kb_id=second["id"])
with db.session() as _s:          # the ingest script writes chunks only; the document record is what lets them be returned
    _s.add(Document(kb_id=second["id"], name="vpn.txt", doc_key="vpn.txt", status="Indexed", chunks=1))
a = retriever.retrieve_scored("reset VPN token", kb_ids=[second["id"]])
b = retriever.retrieve_scored("reset VPN token")
ok(a and a[0]["source"] == "vpn.txt", "search in the IT KB finds its document")
ok(all(h["source"] != "vpn.txt" for h in b), "the default KB does not contain it")
st = kbs.store_for(second)
ok(all(not metadata.missing(m) and m["kb_id"] == second["id"] for _, ms in st.iter_metadata() for m in ms), "ingested chunks have full metadata with the KB id")

# 3. update_metadata changes access without re-embedding
before = st.count()
changed = st.update_metadata({"allowed_groups": metadata.encode_ids([7])}, document="vpn.txt")
after = [m for _, ms in st.iter_metadata() for m in ms]
ok(changed == before and all(m["allowed_groups"] == "|7|" for m in after) and st.count() == before, "update_metadata changes access in place")
ok(len(st.search("VPN", 3, {"doc_id": "vpn.txt"})) > 0 and st.search("VPN", 3, {"doc_id": "nope.txt"}) == [], "filter format works")

# 4. admin API: channel picker choices, validation, binding, delete clean-up
store = ChannelStore(db, FernetSecretStore(generate_key()), lambda t: manager.adapter_class(t).SPEC)
ctx = A.AdminContext(db=db, store=store, manager=manager, signer=SessionSigner(generate_key()), domain="sysfore.com", allow_remote=True)
users.create_admin(db, "boss@sysfore.com", "correct horse battery", domain="sysfore.com")
app = FastAPI(); app.include_router(A.router); app.include_router(kbs_api.router)
app.dependency_overrides[A.get_ctx] = lambda: ctx
c = TestClient(app, base_url="http://localhost")
r = c.post("/admin/api/auth/login", json={"email": "boss@sysfore.com", "password": "correct horse battery"})
h = {"X-CSRF-Token": r.json()["csrf"]}
slack = next(x for x in c.get("/admin/api/channels", headers=h).json() if x["type"] == "slack")
f = next(f for f in slack["fields"] if f["name"] == "kb_ids")
ok(f["kind"] == "kbs" and len(f["choices"]) == 2 and f["value"] == [default["id"]], "channel form lists KBs and preselects the default")
ok(c.put("/admin/api/channels/slack", headers=h, json={"enabled": False, "values": {"kb_ids": [999]}}).status_code == 422, "unknown KB id refused")
ok(c.put("/admin/api/channels/slack", headers=h, json={"enabled": False, "values": {"kb_ids": [second["id"]]}}).status_code == 200, "channel bound to the IT KB")
ok(kbs.channels_using(db, second["id"]) == ["slack"], "binding recorded")
lst = c.get("/admin/api/kbs", headers=h).json()
ok(any(k["name"] == "IT help" and k["documents"] == 1 for k in lst["kbs"]), "KB list shows document count")
ok(c.put(f"/admin/api/kbs/{second['id']}", headers=h, json={"chunk_size": 100, "chunk_overlap": 150}).status_code == 422, "overlap >= size refused")
r = c.delete(f"/admin/api/kbs/{second['id']}", headers=h)
ok(r.status_code == 200 and r.json()["channels_unbound"] == ["slack"] and r.json()["chunks_removed"] > 0, "delete removes vectors and unbinds the channel")
ok(second["collection"] not in [x.name for x in client.list_collections()], "no orphan collection left")
ok(kbs.channels_using(db, second["id"]) == [], "no orphan binding left")
ok(c.delete(f"/admin/api/kbs/{default['id']}", headers=h).status_code == 400, "default KB cannot be deleted")
ok(any(e["action"] == "kb.delete" for e in store.recent_audit()), "deletion audited")
