import os, sys, tempfile
sys.path.insert(0, os.getcwd())
os.environ.setdefault("APP_SECRET_KEY", "")
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.admin import api as A, users, users_api, departments_api
from app.admin.auth import SessionSigner
from app.channels.store import ChannelStore
from app.db import Database
from app.secretstore import FernetSecretStore, generate_key
from tests.test_channels import FakeAdapter, FakeManager

db = Database(os.path.join(tempfile.mkdtemp(), "t.db"))
db.migrate()
store = ChannelStore(db, FernetSecretStore(generate_key()), lambda t: FakeAdapter.SPEC)
ctx = A.AdminContext(db=db, store=store, manager=FakeManager({"fake": ("x", "y")}),
                     signer=SessionSigner(generate_key()), domain="sysfore.com", allow_remote=True)
users.create_admin(db, "boss@sysfore.com", "correct horse battery", domain="sysfore.com")
app = FastAPI()
for r in (A.router, users_api.router, departments_api.router):
    app.include_router(r)
app.dependency_overrides[A.get_ctx] = lambda: ctx
P = "/admin/api"
ok = lambda c, cond, msg: print(("PASS " if cond else "FAIL ") + msg)

admin = TestClient(app, base_url="http://localhost")
r = admin.post(P + "/auth/login", json={"email": "boss@sysfore.com", "password": "correct horse battery"})
h = {"X-CSRF-Token": r.json()["csrf"]}
ok(admin, admin.get(P + "/me/permissions").json()["screens"][:2] == ["channels", "settings"], "admin sees all screens")

depts = admin.get(P + "/departments").json()["departments"]
gid = depts[0]["id"]
r = admin.post(P + "/departments", json={"name": "HR", "retention_days": 30}, headers=h)
hr = r.json()["id"]
ok(admin, r.status_code == 201, "admin adds department HR")
r = admin.post(P + "/departments", json={"name": "X", "retention_days": 9999}, headers=h)
ok(admin, r.status_code == 422, "retention above max refused")

r = admin.post(P + "/users", json={"email": "ed@sysfore.com", "name": "Ed", "role": "editor", "department_id": hr}, headers=h)
ok(admin, r.status_code == 201 and r.json()["link"], "editor created with link")
token, eid = r.json()["link"]["token"], r.json()["user"]["id"]
ok(admin, admin.put(f"{P}/departments/{hr}", json={"editor_ids": [eid]}, headers=h).status_code == 200, "editor assigned to HR")

ed = TestClient(app, base_url="http://localhost")
ok(ed, ed.post(P + "/auth/login", json={"email": "ed@sysfore.com", "password": "x" * 14}).status_code == 401, "editor cannot sign in before setting password")
ok(ed, ed.post(P + "/auth/set-password", json={"token": token, "password": "editor password 1"}).status_code == 200, "set password via link")
ok(ed, ed.post(P + "/auth/set-password", json={"token": token, "password": "editor password 2"}).status_code == 400, "link is single use")
r = ed.post(P + "/auth/login", json={"email": "ed@sysfore.com", "password": "editor password 1"})
eh = {"X-CSRF-Token": r.json()["csrf"]}
ok(ed, ed.get(P + "/me/permissions").json()["screens"] == ["knowledge", "groups", "prompts", "usage", "departments", "account"], "editor sees the Editor screens only (no channels, providers, users, audit, reports)")
ds = ed.get(P + "/departments").json()["departments"]
ok(ed, [d["name"] for d in ds] == ["HR"], "editor sees only own department")
r = ed.put(f"{P}/departments/{hr}", json={"banned_topics": "salaries\n\nSalaries\nlayoffs", "retention_days": 45}, headers=eh)
ok(ed, r.status_code == 200 and r.json()["banned_topics"] == "salaries\nlayoffs", "editor edits topics (normalised) + retention")
ok(ed, ed.put(f"{P}/departments/{hr}", json={"name": "Hax"}, headers=eh).status_code == 403, "editor cannot rename")
ok(ed, ed.put(f"{P}/departments/{gid}", json={"retention_days": 5}, headers=eh).status_code == 404, "editor cannot touch other department")
for path in ("/channels", "/users", "/settings", "/audit"):
    ok(ed, ed.get(P + path).status_code == 403, f"editor blocked from {path}")

ok(admin, admin.put(f"{P}/users/{eid}", json={"status": "Deactivated"}, headers=h).status_code == 200, "admin deactivates editor")
ok(ed, ed.get(P + "/me/permissions").status_code == 401, "deactivated editor's session dies")
me = users.get_user(db, "boss@sysfore.com")
ok(admin, admin.put(f"{P}/users/{me['id']}", json={"role": "viewer"}, headers=h).status_code == 400, "cannot demote self")
ok(admin, admin.delete(f"{P}/departments/{gid}", headers=h).status_code == 400, "default department protected")
ok(admin, admin.delete(f"{P}/departments/{hr}", headers=h).status_code == 400, "department with people protected")
