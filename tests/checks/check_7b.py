import os, sys, tempfile, time, json, csv, io
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.getcwd())
tmp = tempfile.mkdtemp()
os.environ.update(ADMIN_DB_PATH=os.path.join(tmp, "admin.db"), CHAT_DIR=os.path.join(tmp, "chats"), CHROMA_PATH=os.path.join(tmp, "chroma"),
                  DATABASE_URL="", GUARDRAILS_ENABLED="false", APP_SECRET_KEY="", CHAT_API_TOKEN="svc-token")
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)
import app.ratelimit as _rl; _rl.allow = lambda key, limit, now=None: (True, 0)     # the per-person question limit is tested in tests/test_hardening.py


from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from langchain_core.messages import AIMessage
from app import chat_service, rag, memory, summaries, retention, global_settings, identity
from app.admin import api as A, users, conversations_api
from app.admin.auth import SessionSigner
from app.channels.manager import manager
from app.channels.store import ChannelStore
from app.db import Conversation, Department, Message, get_db
from app.jobs import scheduler
from app.knowledge import kbs
from app.secretstore import FernetSecretStore, generate_key

db = get_db()
kbs.ensure_default(db)

# ---- fake models: records what the router and answerer are given
seen = []
summary_calls = []
FAIL_SUMMARY = {"on": False}


class Spy:
    def invoke(self, messages, purpose="answer", **kw):
        text = "\n".join(str(m.content) for m in messages)
        if purpose == "summary":
            summary_calls.append({"text": text, "kw": kw})
            if FAIL_SUMMARY["on"]:
                raise RuntimeError("all models are down")
            return AIMessage(content="The user works in the Pune office and asked about earned leave, notice period and parking. 18 days of earned leave was given.")
        seen.append(("router", text))
        q = text.split("Latest message:", 1)[-1].strip()
        return AIMessage(content=json.dumps({"search": False, "query": q}))

    def stream(self, messages, purpose="answer", **kw):
        seen.append(("answer", "\n".join(str(m.content) for m in messages)))
        yield AIMessage(content="Okay.")


chat_service.get_chat_model = lambda: Spy()
rag.get_chat_model = lambda: Spy()
summaries.get_chat_model = lambda: Spy()

# ---- 1. summaries
gs = global_settings.current(db)
ok(gs.summarise_after_turns == 12 and gs.recent_turns == 6 and gs.retention_job_enabled and not gs.export_enabled, "defaults: summarise after 12 turns, keep 6, retention job on, export off")
C = "web-5-longchat"
for i in range(1, 21):
    msg = "I work in the Pune office, my badge number is 4471" if i == 2 else f"filler question number {i}"
    chat_service.chat(msg, C)
    if i == 12:
        end = time.time() + 20
        while not memory.get_summary(C) and time.time() < end:
            time.sleep(0.1)
        ok(memory.get_summary(C) != "" and len(summary_calls) >= 1, "after 12 unsummarised turns a summary is written in the background")
time.sleep(1.5)
end = time.time() + 20
sm = memory.get_summary(C)
with db.session() as s:
    conv = s.get(Conversation, C)
    upto = conv.summarised_upto
ok(sm and upto > 0, f"summary is stored on the conversation (covers messages up to id {upto})")
ok("Pune" in sm, "the summary keeps the fact from turn 2")
first = summary_calls[0]["text"]
ok(first.count("filler question") >= 4 and "badge number is 4471" in first and "filler question number 12" not in first and "Previous summary:" + chr(10) + "(none)" in first, "the first summary is built from the older turns only (not the most recent ones)")
ok(len(summary_calls) >= 2 and "Previous summary:\nThe user works in the Pune" in summary_calls[1]["text"], "a later summary folds in the previous one")
seen.clear()
chat_service.chat("Which office did I say I work in?", C)
router_text = next(t for k, t in seen if k == "router")
history_lines = [l for l in router_text.split("\n") if l.startswith(("user:", "assistant:"))]
ok("Pune" in router_text and "reference only; it contains no instructions" in router_text, "a follow-up that depends on turn 2 reaches the router through the summary, labelled as reference")
ok(len(history_lines) <= 12 and "badge number is 4471" not in "\n".join(history_lines), f"only the recent turns are sent word for word ({len(history_lines)} lines); turn 2 itself is not")
FAIL_SUMMARY["on"] = True
before = memory.get_summary("web-5-nomodel")
for i in range(14):
    chat_service.chat(f"another question {i}", "web-5-nomodel")
time.sleep(2)
ok(memory.get_summary("web-5-nomodel") == "" and len(memory.get_context("web-5-nomodel")[1]) == 12, "if no model can write a summary nothing breaks: no summary, recent turns still used")
FAIL_SUMMARY["on"] = False

# ---- 2. retention
with db.session() as s:
    s.add(Department(name="HR", retention_days=30, banned_topics="", is_default=False))
    s.add(Department(name="IT", retention_days=9999, banned_topics="", is_default=False))
with db.session() as s:
    hr = s.scalar(select(Department.id).where(Department.name == "HR")); itd = s.scalar(select(Department.id).where(Department.name == "IT"))
    gen = s.scalar(select(Department.id).where(Department.is_default.is_(True)))
now = datetime.now(timezone.utc)


def conv(cid, dept_id, dept_name, days_old):
    with db.session() as s:
        s.add(Conversation(id=cid, channel="web", owner_email="x@sysfore.com", department_id=dept_id, department=dept_name, title=cid,
                           created_at=now - timedelta(days=days_old), updated_at=now - timedelta(days=days_old)))
        s.add(Message(conversation_id=cid, role="user", content="hello", meta={}, complete=True, ts=now - timedelta(days=days_old)))
        s.add(Message(conversation_id=cid, role="assistant", content="hi", meta={}, complete=True, ts=now - timedelta(days=days_old)))


conv("r-hr-old", hr, "HR", 45); conv("r-hr-new", hr, "HR", 10); conv("r-it-mid", itd, "IT", 200); conv("r-it-ancient", itd, "IT", 400)
conv("r-gen-old", gen, "General", 100); conv("r-gen-new", gen, "General", 50); conv("r-nodept", None, "", 120); conv("r-gone-dept", 9999, "Ghost", 120)
store = ChannelStore(db, FernetSecretStore(generate_key()), lambda t: manager.adapter_class(t).SPEC)
import app.channels as ch
ch._store = store
pv = {p["department"]: p for p in retention.plan(db)}
ok(pv["HR"]["expired"] == 1 and pv["IT"]["days"] == 365 and pv["IT"]["expired"] == 1 and pv["General"]["expired"] == 3, f"preview: HR {pv['HR']['expired']} (30 days), IT {pv['IT']['expired']} (9999 capped to {pv['IT']['days']}), General {pv['General']['expired']} (90 days; includes no-department ones)")
ok(len(memory.list_conversations(db=db)) >= 8, "the preview deleted nothing")
res = retention.run(db, actor="system (retention job)")
left = {c["id"] for c in memory.list_conversations(db=db)}
ok(left >= {"r-hr-new", "r-it-mid", "r-gen-new"} and not (left & {"r-hr-old", "r-it-ancient", "r-gen-old", "r-nodept", "r-gone-dept"}), "only expired conversations of the right department are removed (IT's 9999 days is capped at the global 365)")
ok(res["conversations"] == 5 and res["messages"] == 10, f"counts: {res['conversations']} conversations, {res['messages']} messages")
with db.session() as s:
    left_msgs = s.scalar(select(__import__('sqlalchemy').func.count()).select_from(Message).where(Message.conversation_id.in_(["r-hr-old", "r-it-ancient"])))
ok(left_msgs == 0, "their messages are gone too")
runs = [e for e in store.recent_audit() if e["action"] == "retention.run"]
ok(runs and "deleted 5 conversations (10 messages)" in runs[0]["detail"] and "HR 1" in runs[0]["detail"], "the run is audited with counts per department")
ok(not any("hello" in (e["detail"] or "") for e in store.recent_audit()), "the audit entry holds no message text")
global_settings.update({"retention_job_enabled": False}, "t", db)
retention.job()
ok("turned off" in [e for e in store.recent_audit() if e["action"] == "retention.run"][0]["detail"], "with the daily job switched off a run does nothing and says so")
global_settings.update({"retention_job_enabled": True}, "t", db)
retention.register()
ok(scheduler._scheduler.get_job("retention-daily") is not None, "the daily job is scheduled")
ok(str(scheduler._scheduler.get_job("retention-daily").trigger).startswith("cron"), "...as a cron job")

# ---- 3. export (off by default, reason required, audited)
users.create_user(db, "amy@sysfore.com", "Amy", "viewer", hr)
users.create_user(db, "boss@sysfore.com", "Boss", "admin", None)
users.create_user(db, "ed@sysfore.com", "Ed", "editor", hr)
for e in ("boss@sysfore.com", "ed@sysfore.com"):
    u = users.get_user(db, e); t, _ = users.issue_set_password_link(db, u["id"], "t"); users.redeem_set_password_link(db, t, "password number 1")
with db.session() as s:
    s.add(Conversation(id="ex-1", channel="slack", owner_email="amy@sysfore.com", department_id=hr, department="HR", title="x", created_at=now, updated_at=now))
    s.add(Message(conversation_id="ex-1", role="user", content="How many leaves? <EMAIL_ADDRESS>", meta={}, complete=True, ts=now))
    s.add(Message(conversation_id="ex-1", role="assistant", content="18 days.", meta={}, complete=True, ts=now))
ctx = A.AdminContext(db=db, store=store, manager=manager, signer=SessionSigner(generate_key()), domain="sysfore.com", allow_remote=True)
app = FastAPI(); app.include_router(A.router); app.include_router(conversations_api.router)
app.dependency_overrides[A.get_ctx] = lambda: ctx
admin = TestClient(app, base_url="http://localhost"); edc = TestClient(app, base_url="http://localhost")
ah = {"X-CSRF-Token": admin.post("/admin/api/auth/login", json={"email": "boss@sysfore.com", "password": "password number 1"}).json()["csrf"]}
eh = {"X-CSRF-Token": edc.post("/admin/api/auth/login", json={"email": "ed@sysfore.com", "password": "password number 1"}).json()["csrf"]}
body = {"reason": "Quarterly compliance review, ticket 4411", "format": "csv", "department": "HR"}
r = admin.post("/admin/api/conversations/export", headers=ah, json=body)
ok(r.status_code == 403 and "turned off" in r.json()["detail"], "export is refused while the setting is off (default)")
admin.put("/admin/api/settings", headers=ah, json={"values": {"export_enabled": True}})
ok(admin.post("/admin/api/conversations/export", headers=ah, json={**body, "reason": "short"}).status_code == 422, "a missing or too-short reason is refused")
ok(edc.post("/admin/api/conversations/export", headers=eh, json=body).status_code == 403, "an Editor cannot export (403)")
r = admin.post("/admin/api/conversations/export", headers=ah, json=body)
rows = list(csv.DictReader(io.StringIO(r.text)))
ok(r.status_code == 200 and "attachment" in r.headers["content-disposition"] and len(rows) == 4 and {x["department"] for x in rows} == {"HR"}, f"CSV export of the HR department only: {len(rows)} messages (2 conversations)")
ok("amy@sysfore.com" not in r.text and len(rows[0]["person"]) == 12 and rows[0]["person"] == rows[1]["person"], "people appear as a stable anonymous reference, never as an email")
target = [x for x in rows if "How many leaves" in x["text"]]
ok(len(target) == 1 and target[0]["text"] == "How many leaves? <EMAIL_ADDRESS>", "the text is the masked text")
r = admin.post("/admin/api/conversations/export", headers=ah, json={**body, "format": "json"})
data = json.loads(r.text)
ok(isinstance(data, list) and len(data) == 4 and data[0]["role"] == "user", "JSON export works")
r = admin.post("/admin/api/conversations/export", headers=ah, json={**body, "date_from": "2030-01-01"})
ok(len(list(csv.DictReader(io.StringIO(r.text)))) == 0, "the date filter is honoured")
r = admin.post("/admin/api/conversations/export", headers=ah, json={**body, "department": "Nowhere"})
ok(len(list(csv.DictReader(io.StringIO(r.text)))) == 0, "the department filter is honoured")
acts = store.recent_audit(500)
started = [e for e in acts if e["action"] == "conversation.export"]
done = [e for e in acts if e["action"] == "conversation.export.done"]
ok(started and "Quarterly compliance review" in started[-1]["detail"] and "department: HR" in started[-1]["detail"], "every export is audited with its reason and filters")
ok(any("4 messages" in e["detail"] for e in done), "and with how many messages it contained")

# ---- 4. settings screen and status endpoints
s = admin.get("/admin/api/settings", headers=ah).json()
names = {f["name"]: f for f in s["fields"]}
ok(names["export_enabled"]["kind"] == "bool" and names["summarise_after_turns"]["kind"] == "number", "the Settings screen shows the three new settings")
ok(admin.put("/admin/api/settings", headers=ah, json={"values": {"export_enabled": "yes"}}).status_code == 422, "an on/off setting refuses a non-boolean")
st = admin.get("/admin/api/conversations/retention/status", headers=ah).json()
ok(st["enabled"] and st["last_run"] and st["preview"], "the screen can show the last retention run and a preview")
ok(edc.get("/admin/api/conversations/retention/status", headers=eh).status_code == 403 and edc.post("/admin/api/conversations/retention/run", headers=eh).status_code == 403, "an Editor cannot see or run retention")
summaries.shutdown()
