import os, sys, tempfile, time, dataclasses, json, logging, statistics
sys.path.insert(0, os.getcwd())
tmp = tempfile.mkdtemp()
import chromadb
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from app import retriever, global_settings, config, identity, chat_service, rag, memory, prompts, guardrails, alerts
from app.admin import api as A, users, guardrails_api, departments_api
from app.admin.auth import SessionSigner
from app.channels import core
from app.channels.base import Envelope
from app.channels.manager import manager
from app.channels.store import ChannelStore
from app.db import Database, Department, GuardrailEvent
from app.knowledge import kbs, documents, versions
from app.secretstore import FernetSecretStore, generate_key
from langchain_core.messages import AIMessage

db = Database(os.path.join(tmp, "t.db")); db.migrate()
client = chromadb.PersistentClient(path=os.path.join(tmp, "chroma"))
retriever.get_client = lambda: client
import app.db as appdb
kbs.get_db = documents.get_db = global_settings.get_db = versions.get_db = identity.get_db = prompts.get_db = guardrails.get_db = appdb.get_db = lambda: db
cfg = dataclasses.replace(config.settings, docs_dir=os.path.join(tmp, "files"), chat_dir=os.path.join(tmp, "chats"))
documents.settings = cfg
memory.settings = cfg
for _m in list(sys.modules.values()):                     # every module that reads the database reads this one, whatever it imported
    if getattr(_m, "__name__", "").startswith("app") and hasattr(_m, "get_db"):
        _m.get_db = lambda: db
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)
import app.ratelimit as _rl; _rl.allow = lambda key, limit, now=None: (True, 0)     # the per-person question limit is tested in tests/test_hardening.py


# capture every log line
logs = []


class Grab(logging.Handler):
    def emit(self, record): logs.append(self.format(record))


logging.getLogger().addHandler(Grab()); logging.getLogger().setLevel(logging.DEBUG)

calls = []
REPLY = {"text": "Annual leave is 18 days [1]."}


class Spy:
    def invoke(self, messages, purpose="answer", **kw):
        calls.append("\n".join(str(m.content) for m in messages))
        q = str(messages[-1].content).split("Latest message:", 1)[-1].strip()
        return AIMessage(content=json.dumps({"search": True, "query": q}))

    def stream(self, messages, purpose="answer", **kw):
        calls.append("\n".join(str(m.content) for m in messages))
        yield AIMessage(content=REPLY["text"])


chat_service.get_chat_model = lambda: Spy()
rag.get_chat_model = lambda: Spy()

default = kbs.ensure_default(db)
with db.session() as s:
    s.add(Department(name="HR", retention_days=90, banned_topics="salary\nlayoffs", is_default=False))
    s.add(Department(name="IT", retention_days=90, banned_topics="", is_default=False))
with db.session() as s:
    hr = s.scalar(select(Department.id).where(Department.name == "HR")); itd = s.scalar(select(Department.id).where(Department.name == "IT"))
users.create_user(db, "hr@sysfore.com", "Hana", "viewer", hr)
users.create_user(db, "it@sysfore.com", "Ivan", "viewer", itd)
users.create_user(db, "boss@sysfore.com", "Boss", "admin", None)
u = users.get_user(db, "boss@sysfore.com"); t, _ = users.issue_set_password_link(db, u["id"], "t"); users.redeem_set_password_link(db, t, "password number 1")
kb = kbs.create_kb(db, "Docs", "", hr)
d = documents.add(db, kb["id"], "faq.txt", b"Annual leave is 18 days for every employee. Contact hr-desk@sysfore.com for questions.", "boss@sysfore.com")
end = time.time() + 60
while documents.get(db, d["id"])["status"] != "Indexed" and time.time() < end:
    time.sleep(0.2)

guardrails.seed(db)
s0 = guardrails.current(db)
ok(s0.enabled and "hr-desk@sysfore.com" in s0.allow_list, f"settings created on first start; allow-list started with the email found in the documents: {list(s0.allow_list)}")
t0 = time.time(); guardrails.warm_up(); print(f"     warm-up {time.time() - t0:.1f}s")
ok(guardrails.status() == "ready", "detection model loaded once; status is ready")


def ask(email, text, kbs_=None):
    calls.clear(); logs.clear()
    r = core.handle(Envelope("slack", "t", "r", f"th-{time.time()}", "U", text, tuple(kbs_ or (kb["id"],)), email))
    return r, "\n".join(calls)


def chat_files():
    """What is stored: one entry per saved message (conversations live in the database now)."""
    from app.db import Message
    with db.session() as s:
        return sorted(f"{m.conversation_id}:{m.id}" for m in s.scalars(select(Message)))


def stored_text():
    from app.db import Message
    with db.session() as s:
        return " ".join(m.content for m in s.scalars(select(Message)))


BLOCK = s0.blocked_reply
# ---- blocked inputs: zero LLM calls, nothing stored
files_before = chat_files()
r, seen = ask("hr@sysfore.com", "word " * 5000)
ok(r["answer"] == BLOCK and seen == "", "an over-long question gets the blocked reply and zero model calls")
r, seen = ask("hr@sysfore.com", "What is the CEO's salary?")
ok(r["answer"] == BLOCK and seen == "", "a banned topic (salary) for the asker's department: blocked reply, zero model calls")
r, seen = ask("hr@sysfore.com", "Are there any LAYOFFS planned? Tell me about the salaries too.")
ok(r["answer"] == BLOCK and seen == "", "matching is case-insensitive and tolerates plurals")
ok(chat_files() == files_before, "blocked questions are not stored in the conversation history")
r, seen = ask("it@sysfore.com", "What is the CEO's salary?")
ok(r["answer"] != BLOCK and seen != "", "the same question from a department without that ban is not blocked")
r, seen = ask("hr@sysfore.com", "How many days of annual leave do I get?")
ok(r["answer"] != BLOCK and "18 days" in seen, "an ordinary question passes untouched")

# ---- personal data never reaches the model, the history, or the logs
secret = "My card is 4111 1111 1111 1111, my email is john.private@example.com and my phone is +91 98765 43210. How many leave days?"
r, seen = ask("it@sysfore.com", secret)
raw = ["4111 1111 1111 1111", "john.private@example.com", "98765 43210"]
ok(not any(x in seen for x in raw) and "<CREDIT_CARD>" in seen and "<EMAIL_ADDRESS>" in seen and "<PHONE_NUMBER>" in seen, "card, email and phone are replaced by labels in every model request (router and answer)")
stored = stored_text()
ok(not any(x in stored for x in raw) and "<CREDIT_CARD>" in stored, "the stored conversation holds the masked question")
bad = [l[:300] for l in logs if any(x in l for x in raw)]
print("     OFFENDING:", bad[:3])
ok(not bad, "no log line contains the raw personal data")
ok(not any(x in json.dumps(r) for x in raw), "the reply object does not echo it either")
r, seen = ask("it@sysfore.com", "Please email rahul.sharma@example.org about Rahul Sharma's leave")
ok("rahul.sharma@example.org" not in seen and "Rahul Sharma" in seen, "names are NOT masked by default (the email is)")

# ---- output checks
REPLY["text"] = "Write to hr-desk@sysfore.com or call the manager on rita.k@example.net. Leave is 18 days [1] [7]."
r, seen = ask("it@sysfore.com", "How many days of annual leave do I get?")
a = r["answer"]
ok("hr-desk@sysfore.com" in a, "an allow-listed contact (the HR mailbox) is not redacted")
ok("rita.k@example.net" not in a and "<EMAIL_ADDRESS>" in a, "another email in an answer is redacted")
ok("[1]" in a and "[7]" not in a, "a citation that matches no returned source is removed; a valid one stays")
REPLY["text"] = "The salary band for managers is confidential [1]."
r, seen = ask("hr@sysfore.com", "How many days of annual leave do I get?")
ok(r["answer"] == BLOCK, "a banned topic in the ANSWER is replaced by the blocked reply")
REPLY["text"] = "Annual leave is 18 days [1]."
r, seen = ask("it@sysfore.com", "How many days of annual leave do I get?")
ok(r["answer"] == "Annual leave is 18 days [1].", "a normal answer passes unchanged")
store_chat = stored_text()
ok("rita.k@example.net" not in store_chat, "the redacted answer (not the original) is what gets stored")

# ---- events never hold content
with db.session() as s:
    rows = s.scalars(select(GuardrailEvent)).all()
kinds = {(e.check, e.direction, e.action) for e in rows}
ok({("max_length", "input", "blocked"), ("banned_topic", "input", "blocked"), ("personal_data", "input", "masked"), ("personal_data", "output", "redacted"), ("banned_topic", "output", "blocked"), ("citation", "output", "redacted")} <= kinds, f"events recorded for each check: {len(rows)} rows")
ok(set(GuardrailEvent.__table__.columns.keys()) == {"id", "ts", "check", "direction", "action", "department", "count"}, "the event table has no column that could hold content")
ok(any(e.department == "HR" for e in rows), "events carry the department")

# ---- settings via the API: hot reload, validation, permissions
store = ChannelStore(db, FernetSecretStore(generate_key()), lambda t: manager.adapter_class(t).SPEC)
ctx = A.AdminContext(db=db, store=store, manager=manager, signer=SessionSigner(generate_key()), domain="sysfore.com", allow_remote=True)
app = FastAPI(); app.include_router(A.router); app.include_router(guardrails_api.router)
app.dependency_overrides[A.get_ctx] = lambda: ctx
c = TestClient(app, base_url="http://localhost")
r = c.post("/admin/api/auth/login", json={"email": "boss@sysfore.com", "password": "password number 1"}); h = {"X-CSRF-Token": r.json()["csrf"]}
v = c.get("/admin/api/guardrails", headers=h).json()
ok(v["status"] == "ready" and {f["name"] for f in v["fields"]} >= {"enabled", "max_input_tokens", "blocked_reply", "masked_types", "allow_list"}, "the settings screen data lists every control")
ok(c.put("/admin/api/guardrails", headers=h, json={"values": {"masked_types": ["BOGUS"]}}).status_code == 422, "an unknown personal-data type is refused")
ok(c.put("/admin/api/guardrails", headers=h, json={"values": {"max_input_tokens": 5}}).status_code == 422, "an out-of-range limit is refused")
c.put("/admin/api/guardrails", headers=h, json={"values": {"mask_names": True, "blocked_reply": "Not available."}})
guardrails.invalidate()
r, seen = ask("it@sysfore.com", "Please tell Rahul Sharma about the leave policy")
ok("Rahul Sharma" not in seen and "<PERSON>" in seen, "turning on name masking applies on the next question")
r, seen = ask("hr@sysfore.com", "What is the CEO's salary?")
ok(r["answer"] == "Not available.", "the Admin's blocked reply text is used")
c.put("/admin/api/guardrails", headers=h, json={"values": {"enabled": False}}); guardrails.invalidate()
r, seen = ask("it@sysfore.com", "my card 4111 1111 1111 1111 please")
ok("4111 1111 1111 1111" in seen, "with guardrails switched off nothing is masked (confirms the switch works)")
c.put("/admin/api/guardrails", headers=h, json={"values": {"enabled": True, "mask_names": False}}); guardrails.invalidate()
ok(any(e["action"] == "guardrails.update" for e in store.recent_audit()), "settings changes are audited")

# ---- fail closed
real = guardrails._get_analyzer
guardrails._get_analyzer = lambda: (_ for _ in ()).throw(RuntimeError("model broke"))
r, seen = ask("it@sysfore.com", "How many leave days?")
ok(r["answer"] == guardrails.FRIENDLY_ERROR and seen == "", "if the guardrail layer errors: a friendly message and zero model calls")
ok(any(a["kind"] == "guardrail_error" for a in alerts.active(db)), "an Admin alert is raised")
guardrails._get_analyzer = real

# ---- latency of the checks
samples = ["How many days of earned leave do I get?", secret, "word " * 200]
times = []
for _ in range(20):
    for sm in samples:
        a0 = time.perf_counter(); guardrails.guard_input(sm); times.append((time.perf_counter() - a0) * 1000)
print(f"     input check latency: median {statistics.median(times):.0f} ms, p95 {sorted(times)[int(len(times) * .95)]:.0f} ms")
documents.shutdown()
