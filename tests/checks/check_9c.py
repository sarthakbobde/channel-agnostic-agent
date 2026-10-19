import os, sys, tempfile, json
sys.path.insert(0, os.getcwd())
tmp = tempfile.mkdtemp()
from cryptography.fernet import Fernet
os.environ.update(ADMIN_DB_PATH=os.path.join(tmp, "admin.db"), CHAT_DIR=os.path.join(tmp, "chats"), CHROMA_PATH=os.path.join(tmp, "chroma"),
                  DATABASE_URL="", GUARDRAILS_ENABLED="false", APP_SECRET_KEY=Fernet.generate_key().decode(), CHAT_API_TOKEN="x")
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.admin import api as A, users, reports_api
from app.admin.auth import SessionSigner
from app.channels import get_store
from app.channels.manager import manager
from app.db import get_db, GuardrailEvent, Document, Message, Conversation, UsageRecord
from app.knowledge import kbs
from app.secretstore import generate_key

db = get_db()
kb = kbs.ensure_default(db)
store = get_store()
users.create_admin(db, "boss@sysfore.com", "correct horse battery", domain="sysfore.com")
u = users.create_user(db, "ed@sysfore.com", "ed", "editor", None)
t, _ = users.issue_set_password_link(db, u["id"], "t"); users.redeem_set_password_link(db, t, "editor password 1")
ctx = A.AdminContext(db=db, store=store, manager=manager, signer=SessionSigner(generate_key()), domain="sysfore.com", allow_remote=True)
app = FastAPI(); app.include_router(A.router); app.include_router(reports_api.router)
app.dependency_overrides[A.get_ctx] = lambda: ctx
P = "/admin/api"
def login(e, p):
    c = TestClient(app, base_url="http://localhost"); c.post(P + "/auth/login", json={"email": e, "password": p}); return c
admin, ed = login("boss@sysfore.com", "correct horse battery"), login("ed@sysfore.com", "editor password 1")
login("boss@sysfore.com", "wrong password!!")

# ---- audit search
for i in range(60):
    store.record("boss@sysfore.com" if i % 2 else "ed@sysfore.com", "prompt.save" if i % 3 else "kb.create", f"target-{i}", "d")
r = admin.get(P + "/audit/search?limit=50").json()
ok(r["total"] >= 60 and len(r["rows"]) == 50 and "login.failed" in r["actions"] and "boss@sysfore.com" in r["actors"], "audit search pages and lists actors/actions")
r2 = admin.get(P + "/audit/search?limit=50&offset=50").json()
ok(r2["rows"] and r2["rows"][0]["id"] < r["rows"][-1]["id"], "second page continues")
ok(all(x["action"].startswith("prompt.") for x in admin.get(P + "/audit/search?action=prompt.").json()["rows"]), "action prefix filter")
ok(all(x["actor"] == "ed@sysfore.com" for x in admin.get(P + "/audit/search?actor=ED@").json()["rows"]), "actor filter (case-insensitive)")
ok(admin.get(P + "/audit/search?target=target-5").json()["total"] == 11, "target filter")
today = datetime.now(timezone.utc).date().isoformat()
ok(admin.get(P + f"/audit/search?date_from={today}&date_to={today}").json()["total"] >= 60 and admin.get(P + "/audit/search?date_to=2000-01-01").json()["total"] == 0, "date filter")
ok(admin.get(P + "/audit/search?date_from=bad").status_code == 422, "bad date -> 422")
ok(ed.get(P + "/audit/search").status_code == 403, "Editors cannot search the audit log")
ok(len(admin.get(P + "/audit?limit=5").json()) == 5, "old audit list still works")
store.record("boss@sysfore.com", "kb.update", "K" * 200, "long target")
ok(admin.get(P + "/audit/search?action=kb.update").json()["rows"][0]["target"] == "K" * 200, "long audit target is kept whole")

# ---- reports seeded data
now = datetime.now(timezone.utc)
with db.session() as s:
    for chk, d, a, dep in [("injection", "input", "blocked", "HR"), ("injection", "input", "blocked", "HR"), ("pii", "output", "masked", "IT")]:
        s.add(GuardrailEvent(check=chk, direction=d, action=a, department=dep))
    s.add(GuardrailEvent(check="pii", direction="input", action="masked", department="IT", ts=now - timedelta(days=40)))
    s.add(Document(kb_id=kb["id"], name="a.txt", doc_key="a", size=1000, status="Indexed", chunks=4, indexed_at=now))
    s.add(Document(kb_id=kb["id"], name="b.txt", doc_key="b", size=500, status="Failed", chunks=0))
    s.add(Conversation(id="c1", channel="web", department="", updated_at=now))
    for src, ids in [([{"n": 1}], [kb["id"]]), ([{"n": 1}], [kb["id"]]), ([], [kb["id"]])]:
        s.add(Message(conversation_id="c1", role="assistant", content="x", meta={"searched": True, "sources": src, "kb_ids": ids}, complete=True))
    s.add(Message(conversation_id="c1", role="assistant", content="x", meta={"searched": False, "sources": []}, complete=True))
    s.add(UsageRecord(provider="p", model="m", kind="embedding", kb_id=kb["id"], input_tokens=700, purpose="embed"))
g = admin.get(P + "/reports/guardrails").json()
ok(g["total"] == 4 and g["by_check"][0] == {"name": "pii", "count": 2} or g["by_check"][0]["count"] == 2, "guardrail report counts by check")
ok({x["name"]: x["count"] for x in g["by_action"]} == {"blocked": 2, "masked": 2} and {x["name"]: x["count"] for x in g["by_department"]} == {"HR": 2, "IT": 2}, "…by action and department")
g2 = admin.get(P + f"/reports/guardrails?date_from={(now - timedelta(days=7)).date().isoformat()}").json()
ok(g2["total"] == 3, "guardrail report honours the date range")
k = admin.get(P + "/reports/knowledge").json()["rows"][0]
ok(k["chunks"] == 4 and k["size_bytes"] == 1500 and k["by_status"] == {"Indexed": 1, "Failed": 1} and k["last_indexed"], "knowledge report: documents, chunks, size, last indexed")
ok(k["answered"] == 2 and k["not_found"] == 1 and k["embedding_tokens"] == 700, "knowledge report: answered vs not-found, embedding tokens")
c = {x["type"]: x for x in admin.get(P + "/reports/channels").json()["rows"]}
ok(c["web"]["conversations"] == 1 and c["web"]["last_activity"] and "slack" in c and c["slack"]["conversations"] == 0, "channel report")
ok(ed.get(P + "/reports/guardrails").status_code == 403 and ed.get(P + "/reports/knowledge").status_code == 403, "Editors cannot open reports")
ok("reports" in admin.get(P + "/me/permissions").json()["screens"] and "reports" not in ed.get(P + "/me/permissions").json()["screens"], "Reports tab for Admin only")
