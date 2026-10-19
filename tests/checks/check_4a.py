import os, sys, tempfile, time, dataclasses
sys.path.insert(0, os.getcwd())
tmp = tempfile.mkdtemp()
import chromadb
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import retriever, global_settings, config, groups
from app.admin import api as A, users, kbs_api, documents_api, groups_api, departments_api
from app.admin.auth import SessionSigner
from app.channels.manager import manager
from app.channels.store import ChannelStore
from app.db import Database
from app.knowledge import kbs, documents, versions, access, metadata
from app.secretstore import FernetSecretStore, generate_key

db = Database(os.path.join(tmp, "t.db")); db.migrate()
client = chromadb.PersistentClient(path=os.path.join(tmp, "chroma"))
retriever.get_client = lambda: client
kbs.get_db = documents.get_db = global_settings.get_db = versions.get_db = lambda: db
cfg = dataclasses.replace(config.settings, docs_dir=os.path.join(tmp, "files"), max_upload_mb=5)
documents.settings = documents_api.settings = cfg
documents.RETRY_WAIT = (0.01, 0.05)
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)

default = kbs.ensure_default(db)
store = ChannelStore(db, FernetSecretStore(generate_key()), lambda t: manager.adapter_class(t).SPEC)
ctx = A.AdminContext(db=db, store=store, manager=manager, signer=SessionSigner(generate_key()), domain="sysfore.com", allow_remote=True)
users.create_admin(db, "boss@sysfore.com", "correct horse battery", domain="sysfore.com")
hr_dept = departments_api  # noqa
from app.db import Department
with db.session() as s:
    s.add(Department(name="HR", retention_days=90, banned_topics="", is_default=False)); s.add(Department(name="IT", retention_days=90, banned_topics="", is_default=False))
with db.session() as s:
    from sqlalchemy import select
    hr = s.scalar(select(Department.id).where(Department.name == "HR")); it_d = s.scalar(select(Department.id).where(Department.name == "IT"))
amy = users.create_user(db, "amy@sysfore.com", "Amy", "viewer", None)
bob = users.create_user(db, "bob@sysfore.com", "Bob", "viewer", None)
ed = users.create_user(db, "ed@sysfore.com", "Ed", "editor", hr)
ed2 = users.create_user(db, "ed2@sysfore.com", "Ed2", "editor", it_d)
from app.db import DepartmentEditor
with db.session() as s:
    s.add(DepartmentEditor(department_id=hr, user_id=ed["id"])); s.add(DepartmentEditor(department_id=it_d, user_id=ed2["id"]))
for u in (ed, ed2):
    t, _ = users.issue_set_password_link(db, u["id"], "t"); users.redeem_set_password_link(db, t, "editor password 1")

app = FastAPI()
for r in (A.router, kbs_api.router, documents_api.router, groups_api.router):
    app.include_router(r)
app.dependency_overrides[A.get_ctx] = lambda: ctx
P = "/admin/api"


def login(email, pw):
    c = TestClient(app, base_url="http://localhost")
    r = c.post(P + "/auth/login", json={"email": email, "password": pw})
    return c, {"X-CSRF-Token": r.json()["csrf"]}


admin, ah = login("boss@sysfore.com", "correct horse battery")
edc, eh = login("ed@sysfore.com", "editor password 1")
ed2c, e2h = login("ed2@sysfore.com", "editor password 1")
hrkb = admin.post(P + "/kbs", headers=ah, json={"name": "HR docs", "department_id": hr}).json()


def wait(doc_id):
    end = time.time() + 60
    while time.time() < end:
        d = documents.get(db, doc_id)
        if d["status"] in ("Indexed", "Failed"):
            return d
        time.sleep(0.2)


# ---- groups
r = admin.post(P + "/groups", headers=ah, json={"name": "Leadership", "department_id": hr, "member_ids": [amy["id"]]})
g1 = r.json()
ok(r.status_code == 201 and [m["email"] for m in g1["members"]] == ["amy@sysfore.com"], "admin creates a group with a member")
ok(admin.post(P + "/groups", headers=ah, json={"name": "leadership"}).status_code == 422, "duplicate group name refused")
ok(edc.post(P + "/groups", headers=eh, json={"name": "HR team", "department_id": hr, "member_ids": [bob["id"]]}).status_code == 201, "editor creates a group in their own department")
ok(edc.post(P + "/groups", headers=eh, json={"name": "IT team", "department_id": it_d}).status_code == 403, "editor cannot create a group for another department")
ok(edc.post(P + "/groups", headers=eh, json={"name": "No dept"}).status_code == 403, "editor cannot create a group without a department")
ok([g["name"] for g in edc.get(P + "/groups", headers=eh).json()["groups"]] == ["HR team", "Leadership"], "editor sees only groups of their department")
ok(ed2c.get(P + "/groups", headers=e2h).json()["groups"] == [], "editor of another department sees none")
ok(ed2c.put(P + f"/groups/{g1['id']}", headers=e2h, json={"name": "x"}).status_code == 404, "other department's editor cannot edit it")
r = edc.put(P + f"/groups/{g1['id']}", headers=eh, json={"member_ids": [amy["id"], bob["id"]]})
ok(r.status_code == 200 and len(r.json()["members"]) == 2, "editor adds a member")
ok(len(edc.get(P + "/directory", headers=eh).json()) >= 4, "directory lists active people")

# ---- documents with access
text = b"The executive bonus pool for 2026 is reserved for the leadership team and is confidential."
r = admin.post(P + f"/kbs/{hrkb['id']}/documents", headers=ah, files={"file": ("bonus.txt", text)},
               data={"visibility": "groups", "group_ids": str(g1["id"])})
ok(r.status_code == 201 and r.json()["visibility"] == "groups", "upload restricted to a group from the start")
did = r.json()["id"]; wait(did)
chunks = [m for _, ms in kbs.store_for(kbs.get_kb(db, hrkb["id"])).iter_metadata() for m in ms]
ok(chunks and all(m["allowed_groups"] == f"|{g1['id']}|" and m["allowed_people"] == "" and not metadata.missing(m) for m in chunks), "restriction is stored in every chunk from the first moment it is indexed")
a = admin.get(P + f"/documents/{did}/access", headers=ah).json()
ok(a == {"visibility": "groups", "group_ids": [g1["id"]], "user_ids": []}, "access can be read back")

# change access in place: no re-embedding
store_ = kbs.store_for(kbs.get_kb(db, hrkb["id"]))
before_ids = sorted(i for ids, _ in store_.iter_metadata() for i in ids)
r = edc.put(P + f"/documents/{did}/access", headers=eh, json={"visibility": "people", "user_ids": [bob["id"]]})
after = [m for _, ms in store_.iter_metadata() for m in ms]
ok(r.status_code == 200 and all(m["allowed_people"] == f"|{bob['id']}|" and m["allowed_groups"] == "" for m in after), "editor switches to selected people: chunk metadata updated in place")
ok(sorted(i for ids, _ in store_.iter_metadata() for i in ids) == before_ids and documents.get(db, did)["status"] == "Indexed", "no re-embedding (same chunk ids, still Indexed)")
r = edc.put(P + f"/documents/{did}/access", headers=eh, json={"visibility": "everyone"})
after = [m for _, ms in store_.iter_metadata() for m in ms]
ok(r.status_code == 200 and all(m["allowed_groups"] == "" and m["allowed_people"] == "" for m in after), "back to Everyone clears the restriction")
ok(edc.put(P + f"/documents/{did}/access", headers=eh, json={"visibility": "groups", "group_ids": []}).status_code == 422, "restricted with nobody chosen is refused (never silently open)")
ok(edc.put(P + f"/documents/{did}/access", headers=eh, json={"visibility": "groups", "group_ids": [999]}).status_code == 422, "unknown group refused")
ok(ed2c.put(P + f"/documents/{did}/access", headers=e2h, json={"visibility": "everyone"}).status_code == 404, "editor of another department cannot change access")
ok(ed2c.post(P + f"/kbs/{hrkb['id']}/documents", headers=e2h, files={"file": ("x.txt", b"hello")}).status_code == 404, "editor of another department cannot upload into this KB")

# restriction survives re-index, replace, rebuild and move
admin.put(P + f"/documents/{did}/access", headers=ah, json={"visibility": "groups", "group_ids": [g1["id"]]})
admin.post(P + f"/documents/{did}/reindex", headers=ah); wait(did)
ok(all(m["allowed_groups"] == f"|{g1['id']}|" for _, ms in store_.iter_metadata() for m in ms), "restriction survives a re-index")
admin.post(P + f"/documents/{did}/replace", headers=ah, files={"file": ("bonus.txt", text + b" Updated.")}); wait(did)
ok(all(m["allowed_groups"] == f"|{g1['id']}|" for _, ms in store_.iter_metadata() for m in ms), "restriction survives a replace")
admin.put(P + f"/kbs/{hrkb['id']}", headers=ah, json={"chunk_size": 300, "chunk_overlap": 30})
admin.post(P + f"/kbs/{hrkb['id']}/index/rebuild", headers=ah)
end = time.time() + 60
while versions.rebuilding(db, hrkb["id"]) and time.time() < end:
    time.sleep(0.2)
store2 = kbs.store_for(kbs.get_kb(db, hrkb["id"]))
ok(versions.active(db, hrkb["id"])["version"] == 2 and all(m["allowed_groups"] == f"|{g1['id']}|" for _, ms in store2.iter_metadata() for m in ms), "restriction survives an index rebuild")
other = admin.post(P + "/kbs", headers=ah, json={"name": "Other", "department_id": hr}).json()
admin.post(P + f"/documents/{did}/move", headers=ah, json={"kb_id": other["id"]}); wait(did)
ok(all(m["allowed_groups"] == f"|{g1['id']}|" for _, ms in kbs.store_for(kbs.get_kb(db, other["id"])).iter_metadata() for m in ms), "restriction survives a move to another KB")

# ---- group deletion is blocked while a document depends on it
r = admin.delete(P + f"/groups/{g1['id']}", headers=ah)
ok(r.status_code == 409 and "bonus.txt" in r.json()["detail"], "a group in use by a document cannot be deleted")
admin.put(P + f"/documents/{did}/access", headers=ah, json={"visibility": "everyone"})
ok(admin.delete(P + f"/groups/{g1['id']}", headers=ah).status_code == 200, "after changing the document's access, the group can be deleted")

# ---- deleting a document removes its access rows; failed restricted upload leaves nothing open
admin.put(P + f"/documents/{did}/access", headers=ah, json={"visibility": "people", "user_ids": [amy["id"]]})
admin.delete(P + f"/documents/{did}", headers=ah)
from app.db import DocumentAccess
with db.session() as s:
    left = s.scalars(select(DocumentAccess).where(DocumentAccess.document_id == did)).all()
ok(left == [], "deleting a document removes its access rows")
r = admin.post(P + f"/kbs/{other['id']}/documents", headers=ah, files={"file": ("secret.txt", b"Top secret plan.")}, data={"visibility": "groups", "group_ids": "9999"})
ok(r.status_code == 422 and documents.list_for_kb(db, other["id"]) == [], "upload with an invalid restriction is refused and nothing is left behind")
documents.shutdown()
