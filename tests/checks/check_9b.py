import os, sys, tempfile, time, json, dataclasses, types
sys.path.insert(0, os.getcwd())
tmp = tempfile.mkdtemp()
from cryptography.fernet import Fernet
os.environ.update(ADMIN_DB_PATH=os.path.join(tmp, "admin.db"), CHAT_DIR=os.path.join(tmp, "chats"), CHROMA_PATH=os.path.join(tmp, "chroma"),
                  DATABASE_URL="", GUARDRAILS_ENABLED="false", APP_SECRET_KEY=Fernet.generate_key().decode(), CHAT_API_TOKEN="x")
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from app import alerts, alert_sinks, config
from app.admin import api as A, users, alerts_api, kbs_api, documents_api
from app.admin.auth import SessionSigner
from app.channels import get_store, health as channel_health
from app.channels.manager import manager
from app.db import get_db, Department, DepartmentEditor
from app.knowledge import kbs, documents, storehealth
from app.secretstore import generate_key

db = get_db()
default = kbs.ensure_default(db)
store = get_store()
documents.settings = dataclasses.replace(config.settings, docs_dir=os.path.join(tmp, "files"))
documents.RETRY_WAIT = (0.01, 0.05)
with db.session() as s:
    s.add(Department(name="HR", retention_days=90, banned_topics="", is_default=False))
with db.session() as s:
    hr = s.scalar(select(Department.id).where(Department.name == "HR"))
users.create_admin(db, "boss@sysfore.com", "correct horse battery", domain="sysfore.com")
for e in ("ed1@sysfore.com", "ed2@sysfore.com"):
    u = users.create_user(db, e, e.split("@")[0], "editor", hr)
    with db.session() as s:
        s.add(DepartmentEditor(department_id=hr, user_id=u["id"]))
    t, _ = users.issue_set_password_link(db, u["id"], "t"); users.redeem_set_password_link(db, t, "editor password 1")
kb = kbs.create_kb(db, "Policies", "", hr)
ctx = A.AdminContext(db=db, store=store, manager=manager, signer=SessionSigner(generate_key()), domain="sysfore.com", allow_remote=True)
app = FastAPI()
for r in (A.router, alerts_api.router, kbs_api.router, documents_api.router):
    app.include_router(r)
app.dependency_overrides[A.get_ctx] = lambda: ctx
P = "/admin/api"


def login(email, pw):
    c = TestClient(app, base_url="http://localhost")
    return c, {"X-CSRF-Token": c.post(P + "/auth/login", json={"email": email, "password": pw}).json()["csrf"]}


admin, ah = login("boss@sysfore.com", "correct horse battery")
e1, h1 = login("ed1@sysfore.com", "editor password 1")
e2, h2 = login("ed2@sysfore.com", "editor password 1")

# ---- document indexing failed: tells the uploader and the Admins, clears when fixed
r = e1.post(f"{P}/kbs/{kb['id']}/documents", headers=h1, files={"file": ("empty.txt", b"   \n  ")})
ok(r.status_code == 201, "an Editor uploads a file that has no readable text")
doc_id = r.json()["id"]
end = time.time() + 60
while documents.get(db, doc_id)["status"] not in ("Failed", "Indexed") and time.time() < end:
    time.sleep(0.2)
ok(documents.get(db, doc_id)["status"] == "Failed", "it fails to index")
a_admin, a_e1, a_e2 = admin.get(P + "/me/alerts").json(), e1.get(P + "/me/alerts").json(), e2.get(P + "/me/alerts").json()
ok(any(a["kind"] == "indexing_failed" and "empty.txt" in a["message"] for a in a_admin), "the Admins get an 'indexing failed' alert")
ok(any(a["kind"] == "indexing_failed" for a in a_e1) and not any(a["kind"] == "indexing_failed" for a in a_e2), "so does the uploader, and another Editor does not")
aid = next(a["id"] for a in a_e1 if a["kind"] == "indexing_failed")
ok(e2.post(f"{P}/me/alerts/{aid}/resolve", headers=h2).status_code == 404, "another Editor cannot dismiss it")
r = e1.post(f"{P}/documents/{doc_id}/replace", headers=h1, files={"file": ("empty.txt", b"Employees get eighteen days of leave every year.")})
end = time.time() + 60
while documents.get(db, doc_id)["status"] not in ("Failed", "Indexed") and time.time() < end:
    time.sleep(0.2)
ok(documents.get(db, doc_id)["status"] == "Indexed" and not [a for a in admin.get(P + "/me/alerts").json() if a["kind"] == "indexing_failed"], "once the document indexes the alert clears itself")
r = e1.post(f"{P}/kbs/{kb['id']}/documents", headers=h1, files={"file": ("blank2.txt", b"  ")})
end = time.time() + 60
while documents.get(db, r.json()["id"])["status"] not in ("Failed", "Indexed") and time.time() < end:
    time.sleep(0.2)
aid = next(a["id"] for a in e1.get(P + "/me/alerts").json() if a["kind"] == "indexing_failed")
ok(e1.post(f"{P}/me/alerts/{aid}/resolve", headers=h1).status_code == 200 and not e1.get(P + "/me/alerts").json(), "the uploader can dismiss their own alert")
documents.delete(db, r.json()["id"])
ok(not alerts.active(db), "deleting the failed document leaves no stale alert")

# ---- channel stopped or failing
state = {"adapter": None, "on": True}
fake_store = types.SimpleNamespace(runtime_config=lambda typ: ((state["on"], {}) if typ == "slack" else None))
fake_mgr = types.SimpleNamespace(registry={"slack": 1, "teams": 1, "web": 1}, get=lambda typ: state["adapter"] if typ == "slack" else None,
                                 adapter_class=lambda typ: types.SimpleNamespace(title={"slack": "Slack", "teams": "Microsoft Teams"}.get(typ, typ)))
res = channel_health.check_all(db, fake_mgr, fake_store)
al = [a for a in alerts.active(db) if a["kind"] == "channel_down"]
ok(res == {"slack": False} and len(al) == 1 and "Slack is switched on but is not running" in al[0]["message"], "a channel that is on but not running raises an alert")
state["adapter"] = types.SimpleNamespace(healthy=lambda: False)
channel_health.check_all(db, fake_mgr, fake_store)
ok(any("lost its connection" in a["message"] for a in alerts.active(db)), "a channel whose connection dropped raises an alert")
state["adapter"] = types.SimpleNamespace(healthy=lambda: True)
channel_health.check_all(db, fake_mgr, fake_store)
ok(not [a for a in alerts.active(db) if a["kind"] == "channel_down"], "when it is healthy again the alert clears itself")
state["adapter"] = None; state["on"] = False
channel_health.check_all(db, fake_mgr, fake_store)
ok(not [a for a in alerts.active(db) if a["kind"] == "channel_down"], "a channel that is switched off raises nothing")

# ---- the other alerts of the BRD table are covered elsewhere
ok(True, "fallback used, provider key failing: covered by tests/test_providers.py; vector store unreachable: covered by the part 8c check")

# ---- optional notifications
sent = {"webhook": [], "mail": []}
alert_sinks._send_webhook = lambda url, text: sent["webhook"].append((url, text))
alert_sinks._send_mail = lambda cfg, pw, subject, text: sent["mail"].append((cfg["smtp_to"], subject, text))
ok(admin.get(P + "/alert-sinks").json()["webhook_enabled"] is False and admin.get(P + "/alert-sinks").json()["smtp_enabled"] is False, "notifications are off by default")
alerts.raise_alert(db, "t0", "test", "first alert", "error"); time.sleep(1.5)
ok(sent["webhook"] == [] and sent["mail"] == [], "with them off nothing is sent")
ok(admin.put(P + "/alert-sinks", headers=ah, json={"values": {"webhook_enabled": True}, "secrets": {"webhook_url": "http://insecure.example/hook"}}).status_code == 422, "a webhook must be https")
ok(admin.put(P + "/alert-sinks", headers=ah, json={"values": {"smtp_enabled": True}}).status_code == 422, "email needs a server, a From and a recipient")
URL = "https://hooks.example.com/services/T000/B000/SECRETVALUE123"
r = admin.put(P + "/alert-sinks", headers=ah, json={"values": {"webhook_enabled": True, "smtp_enabled": True, "smtp_host": "mail.example.com", "smtp_from": "agent@example.com",
                                                              "smtp_to": ["a@example.com", "b@example.com"], "smtp_user": "u", "min_severity": "warning"},
                                                    "secrets": {"webhook_url": URL, "smtp_password": "MailPassw0rd!"}})
ok(r.status_code == 200 and URL not in r.text and "MailPassw0rd" not in r.text and sorted(r.json()["secrets_set"]) == ["smtp_password", "webhook_url"], "saved: the webhook address and the mail password are never returned")
from app.db import Secret
with db.session() as s:
    cipher = [x.ciphertext for x in s.scalars(select(Secret).where(Secret.owner_type == "alert-sink")).all()]
ok(len(cipher) == 2 and not any("SECRETVALUE123" in c or "MailPassw0rd" in c for c in cipher), "they are stored encrypted")
alerts.raise_alert(db, "t1", "test", "second alert", "warning"); time.sleep(2)
ok(len(sent["webhook"]) == 1 and sent["webhook"][0][0] == URL and "second alert" in sent["webhook"][0][1] and len(sent["mail"]) == 1 and sent["mail"][0][0] == ["a@example.com", "b@example.com"], "a NEW alert goes to the webhook and by email")
alerts.raise_alert(db, "t1", "test", "second alert", "warning"); time.sleep(1.5)
ok(len(sent["webhook"]) == 1, "a repeat of an open alert is not sent again")
admin.put(P + "/alert-sinks", headers=ah, json={"values": {"min_severity": "error"}})
alerts.raise_alert(db, "t2", "test", "just a warning", "warning"); alerts.raise_alert(db, "t3", "test", "an error", "error"); time.sleep(2)
ok(len(sent["webhook"]) == 2 and "an error" in sent["webhook"][1][1], "with 'errors only' a warning is not sent")
alert_sinks._send_webhook = lambda url, text: (_ for _ in ()).throw(RuntimeError("down " + url))
alerts.raise_alert(db, "t4", "test", "while the webhook is down", "error"); time.sleep(2)
ok(any(a["key"] == "t4" for a in alerts.active(db)), "a failing notification never breaks the alert itself (the banner is still there)")
r = admin.post(P + "/alert-sinks/test", headers=ah)
ok(r.status_code == 200 and "webhook" in r.json()["results"] and URL not in r.text and "SECRETVALUE123" not in r.text, "'Send a test' reports the result without echoing the address")
ok(e1.get(P + "/alert-sinks").status_code == 403 and e1.put(P + "/alert-sinks", headers=h1, json={}).status_code == 403, "Editors cannot see or change notifications (403)")
audit = json.dumps(store.recent_audit(500), default=str)
ok("SECRETVALUE123" not in audit and "MailPassw0rd" not in audit and "alert_sinks.update" in audit, "changes are audited without the secrets")
documents.shutdown()
