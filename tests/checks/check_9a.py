import os, sys, tempfile, json
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.getcwd())
tmp = tempfile.mkdtemp()
from cryptography.fernet import Fernet
os.environ.update(ADMIN_DB_PATH=os.path.join(tmp, "admin.db"), CHAT_DIR=os.path.join(tmp, "chats"), CHROMA_PATH=os.path.join(tmp, "chroma"),
                  DATABASE_URL="", GUARDRAILS_ENABLED="false", APP_SECRET_KEY=Fernet.generate_key().decode(), CHAT_API_TOKEN="x")
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app import usage
from app.admin import api as A, users, usage_api
from app.admin.auth import SessionSigner
from app.channels import get_store
from app.channels.manager import manager
from app.db import get_db, UsageRecord, ModelPrice, Department, DepartmentEditor
from app.knowledge import kbs
from app.secretstore import generate_key

db = get_db()
default = kbs.ensure_default(db)
store = get_store()
with db.session() as s:
    s.add(Department(name="HR", retention_days=90, banned_topics="", is_default=False)); s.add(Department(name="IT", retention_days=90, banned_topics="", is_default=False))
with db.session() as s:
    hr = s.scalar(select(Department.id).where(Department.name == "HR")); itd = s.scalar(select(Department.id).where(Department.name == "IT"))
users.create_admin(db, "boss@sysfore.com", "correct horse battery", domain="sysfore.com")
ed = users.create_user(db, "ed@sysfore.com", "Ed", "editor", hr)
with db.session() as s:
    s.add(DepartmentEditor(department_id=hr, user_id=ed["id"]))
u = users.get_user(db, "ed@sysfore.com"); t, _ = users.issue_set_password_link(db, u["id"], "t"); users.redeem_set_password_link(db, t, "editor password 1")
kb = kbs.create_kb(db, "Policies", "", hr)
now = datetime.now(timezone.utc)

with db.session() as s:
    s.add(ModelPrice(provider_type="google", model="gemini-flash", input_per_1k=0.1, output_per_1k=0.4))


def rec(**kw):
    base = dict(provider="Gemini", provider_type="google", model="gemini-flash", purpose="answer", department="HR", input_tokens=1000, output_tokens=500, status="ok")
    base.update(kw)
    usage.record(db, **base)


rec(); rec(purpose="router", input_tokens=200, output_tokens=20); rec(department="IT")                       # priced: 0.1+0.2 = 0.3, 0.02+0.008, 0.3
rec(provider="Grok", provider_type="xai", model="grok-2", input_tokens=400, output_tokens=100)               # no price -> unpriced
rec(status="failed", input_tokens=None, output_tokens=None, error="HTTP 429")
rec(provider="Local MiniLM (built in)", provider_type="chroma-default", model="chroma-default", department=None, purpose="embed-doc", kind="embedding", kb_id=kb["id"], input_tokens=300, output_tokens=0, cost=0.0)
rec(provider="Cohere", provider_type="cohere", model="embed-multilingual-v3.0", department=None, purpose="embed-doc", kind="embedding", kb_id=kb["id"], input_tokens=900, output_tokens=0)
# older rows for the date filters and the day / month grouping
with db.session() as s:
    old = s.scalars(select(UsageRecord).order_by(UsageRecord.id).limit(2)).all()
    old[0].ts = now - timedelta(days=40); old[1].ts = now - timedelta(days=3)

ctx = A.AdminContext(db=db, store=store, manager=manager, signer=SessionSigner(generate_key()), domain="sysfore.com", allow_remote=True)
app = FastAPI(); app.include_router(A.router); app.include_router(usage_api.router)
app.dependency_overrides[A.get_ctx] = lambda: ctx
admin = TestClient(app, base_url="http://localhost"); edc = TestClient(app, base_url="http://localhost")
ah = {"X-CSRF-Token": admin.post("/admin/api/auth/login", json={"email": "boss@sysfore.com", "password": "correct horse battery"}).json()["csrf"]}
eh = {"X-CSRF-Token": edc.post("/admin/api/auth/login", json={"email": "ed@sysfore.com", "password": "editor password 1"}).json()["csrf"]}
P = "/admin/api"
g = lambda c, **q: c.get(P + "/usage", params=q).json()

r = g(admin, group="provider")
raw_cost = db.session().__enter__().scalar(select(func.sum(UsageRecord.cost)))
ok(abs(r["totals"]["cost"] - round(float(raw_cost), 6)) < 1e-9 and r["totals"]["calls"] == 7, f"totals match the table: {r['totals']['calls']} calls, ${r['totals']['cost']}")
by = {x["label"]: x for x in r["rows"]}
ok(by["Gemini"]["calls"] == 4 and by["Gemini"]["failed_calls"] == 1 and by["Gemini"]["input_tokens"] == 2200 and abs(by["Gemini"]["cost"] - (0.3 + 0.028 + 0.3)) < 1e-9, "by provider: calls, failed calls, tokens and cost add up")
ok(by["Grok"]["cost"] == 0 and by["Grok"]["unpriced_calls"] == 1, "a model without a price is counted as unpriced, not as free")
ok(by["Local MiniLM (built in)"]["unpriced_calls"] == 0 and by["Cohere"]["unpriced_calls"] == 1, "a local model costs $0 and is not unpriced; a paid embedding model without a price is")
ok(r["totals"]["unpriced_calls"] == 2, "the total shows how many calls are unpriced")
rm = {x["label"]: x for x in g(admin, group="model")["rows"]}
ok(set(rm) == {"gemini-flash", "grok-2", "chroma-default", "embed-multilingual-v3.0"}, "by model")
rd = {x["label"]: x for x in g(admin, group="department")["rows"]}
print("     DEPARTMENTS:", {k: v["calls"] for k, v in rd.items()})
ok(rd["HR"]["calls"] == 4 and rd["IT"]["calls"] == 1 and "No department" in rd, "by department (embedding documents have none)")
rk = {x["label"]: x for x in g(admin, group="kb")["rows"]}
ok(rk["Policies"]["calls"] == 2 and rk["Policies"]["input_tokens"] == 1200 and "No knowledge base" in rk, "by knowledge base: embedding usage is attributed to it")
ok({x["label"] for x in g(admin, group="kind")["rows"]} == {"llm", "embedding"} and {x["label"] for x in g(admin, group="purpose")["rows"]} >= {"answer", "router", "embed-doc"}, "by kind and by purpose")
days = g(admin, group="day")["rows"]
ok([d["label"] for d in days] == sorted(d["label"] for d in days) and len(days) == 3, f"by day: {[d['label'] for d in days]}")
months = g(admin, group="month")["rows"]
ok(len(months) >= 1 and sum(m["calls"] for m in months) == 7, "by month")
last7 = g(admin, group="provider", date_from=(now - timedelta(days=7)).strftime("%Y-%m-%d"))
ok(last7["totals"]["calls"] == 6, "the period filter leaves out the 40-day-old call")
ok(g(admin, group="provider", date_from="2030-01-01")["rows"] == [], "an empty period returns nothing")
ok(admin.get(P + "/usage", params={"group": "nonsense"}).status_code == 422 and admin.get(P + "/usage", params={"date_from": "garbage"}).status_code == 422, "bad group or date is refused")

# Editors see their own departments only
re_ = g(edc, group="department")
ok([x["label"] for x in re_["rows"]] == ["HR"] and re_["scope"] == "your departments", "an Editor sees only their department's usage")
ok(g(edc, group="provider", department="IT")["rows"] == [], "an Editor cannot ask for another department by naming it")
ok(edc.get(P + "/prices").status_code == 403 and edc.put(P + "/prices", headers=eh, json={"provider_type": "x", "model": "y", "input_per_1k": 1}).status_code == 403, "an Editor cannot see or change prices (403)")

# price list
pl = admin.get(P + "/prices").json()
ok(len(pl["prices"]) == 1 and {(u["provider"], u["model"]) for u in pl["unpriced"]} == {("Grok", "grok-2"), ("Cohere", "embed-multilingual-v3.0")}, "the price list shows what is priced and which used models are not")
r = admin.put(P + "/prices", headers=ah, json={"provider_type": "xai", "model": "grok-2", "input_per_1k": 0.2, "output_per_1k": 1.0})
ok(r.status_code == 200, "an Admin sets a price")
rec(provider="Grok", provider_type="xai", model="grok-2", input_tokens=1000, output_tokens=1000)
ok(abs({x["label"]: x for x in g(admin, group="provider")["rows"]}["Grok"]["cost"] - 1.2) < 1e-9, "calls from now on are priced (1000 in x 0.2 + 1000 out x 1.0 per 1k = $1.20)")
admin.put(P + "/prices", headers=ah, json={"provider_type": "xai", "model": "grok-2", "input_per_1k": 0.3, "output_per_1k": 1.0})
ok(len(admin.get(P + "/prices").json()["prices"]) == 2, "setting the same model again updates it instead of adding another")
ok(admin.put(P + "/prices", headers=ah, json={"provider_type": "xai", "model": "grok-2", "input_per_1k": -1}).status_code == 422 and admin.put(P + "/prices", headers=ah, json={"provider_type": "", "model": "x", "input_per_1k": 1}).status_code == 422, "negative or incomplete prices are refused")
pid = next(p["id"] for p in admin.get(P + "/prices").json()["prices"] if p["model"] == "grok-2")
ok(admin.delete(f"{P}/prices/{pid}", headers=ah).status_code == 200 and admin.delete(f"{P}/prices/{pid}", headers=ah).status_code == 404, "a price can be removed")
acts = [e["action"] for e in store.recent_audit(200)]
ok(acts.count("price.set") == 2 and "price.delete" in acts, "price changes are audited (old and new value)")
