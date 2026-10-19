"""Scenario: things go wrong, and the rules that keep records.
Resilience: the preferred model fails (402, 429, 5xx, timeout) -> the fallback answers and a banner appears; a rejected request is
not retried elsewhere; both fail -> a friendly reply and an alert; recovery clears the alerts; one bad channel credential leaves the
other channels running; an unreachable vector store shows its status and raises an alert.
Governance: retention deletes only what is expired and is audited; an export needs a reason and is audited; the audit log has no secrets."""
import csv
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _scenario import boot, HTTPError

w = boot()
ok, db, P = w.ok, w.db, "/admin/api"
import httpx
from sqlalchemy import select
from app import alerts, retention
from app.channels import core
from app.db import Conversation, Department, Message
from app.knowledge import storehealth

w.users.create_admin(db, "boss@sysfore.com", "correct horse battery", domain="sysfore.com")
with db.session() as s:
    s.add(Department(name="HR", retention_days=30, banned_topics="", is_default=False))
with db.session() as s:
    hr = s.scalar(select(Department.id).where(Department.name == "HR"))
w.person("amy@sysfore.com", "viewer", hr)
admin, ah = w.login("boss@sysfore.com", "correct horse battery")
admin.put(P + "/providers/approval", headers=ah, json={"approved": ["openai", "anthropic"]})
p1 = admin.post(P + "/providers", headers=ah, json={"type": "openai", "name": "Main", "values": {"api_key": "sk-main-111111"}}).json()["id"]
p2 = admin.post(P + "/providers", headers=ah, json={"type": "anthropic", "name": "Backup", "values": {"api_key": "sk-backup-222222"}}).json()["id"]
admin.put(P + "/model-settings", headers=ah, json={"preferred": {"provider_id": p1, "model": "model-a"}, "fallback": {"provider_id": p2, "model": "model-b"}, "request_timeout": 30})
kb = admin.post(P + "/kbs", headers=ah, json={"name": "HR docs", "department_id": hr}).json()
doc = admin.post(f"{P}/kbs/{kb['id']}/documents", headers=ah, files={"file": ("leave.txt", b"Annual leave is 18 days for every employee.")}).json()
w.wait_doc(doc["id"])
main, backup = w.models["openai"], w.models["anthropic"]
main.answer = "Main model: 18 days [1]."
backup.answer = "Backup model: 18 days [1]."
Q = "How many days of annual leave?"


def banner():
    return {a["key"] for a in admin.get(P + "/me/alerts").json()}


res = w.ask("amy@sysfore.com", Q, kb_ids=[kb["id"]])
ok(res["answer"].startswith("Main model") and not banner(), "normally the preferred model answers and there is no banner")

# ---- the preferred model fails in each way the fallback rule names
for label, exc in [("402 no credit", HTTPError(402)), ("429 rate limit", HTTPError(429)), ("503 server error", HTTPError(503)),
                   ("a timeout", httpx.ReadTimeout("slow"))]:
    main.fail, backup.calls = exc, 0
    res = w.ask("amy@sysfore.com", Q, kb_ids=[kb["id"]])
    ok(res["answer"].startswith("Backup model") and backup.calls >= 1, f"{label}: the fallback model answers")
    ok("fallback_used" in banner(), f"{label}: the Admin sees a 'fallback used' banner")
ok("provider_failing:" + "Main" in " ".join(banner()) or any(k.startswith("provider_failing") for k in banner()), "a failing provider (credit) is flagged to the Admin")
main.fail, backup.calls = None, 0
res = w.ask("amy@sysfore.com", Q, kb_ids=[kb["id"]])
ok(res["answer"].startswith("Main model") and not banner(), "when the preferred model works again the banners clear themselves")

# ---- an invalid request is not retried elsewhere; both models failing gives a friendly reply
main.fail, backup.calls = HTTPError(400), 0
res = w.ask("amy@sysfore.com", Q, kb_ids=[kb["id"]])
ok(backup.calls == 0 and res["answer"] == core.ERROR_REPLY, "a rejected request (400) is not sent to the fallback; the person gets a friendly reply")
main.fail, backup.fail = HTTPError(402), HTTPError(503)
res = w.ask("amy@sysfore.com", Q, kb_ids=[kb["id"]])
ok(res["answer"] == core.ERROR_REPLY and "llm_unavailable" in banner(), "both models failing: a friendly reply and an alert for the Admin")
ok("402" not in res["answer"] and "503" not in res["answer"] and "sk-" not in res["answer"], "the person is never shown technical detail or a key")
main.fail = backup.fail = None
w.ask("amy@sysfore.com", Q, kb_ids=[kb["id"]])
ok("llm_unavailable" not in banner(), "the alert clears once a model answers again")

# ---- one bad channel credential leaves the others running
ok(admin.put(P + "/channels/slack", headers=ah, json={"enabled": True, "values": {"bot_token": "xoxb-good", "app_token": "xapp-good"}}).status_code == 200, "Slack is running with a good credential")
admin.put(P + "/channels/web", headers=ah, json={"enabled": True, "values": {}})
r = admin.put(P + "/channels/slack", headers=ah, json={"enabled": True, "values": {"bot_token": "xoxb-bad", "app_token": "xapp-bad"}})
ok(r.status_code in (400, 422) and "xoxb-bad" not in r.text, "a bad credential is refused with a message that does not echo it")
ok(w.manager.get("slack") is not None and w.manager.get("slack").cfg["bot_token"] == "xoxb-good", "the working Slack connection is still the old one")
ch = {c["type"]: c for c in admin.get(P + "/channels").json()}
ok(ch["slack"]["running"] and ch["slack"]["enabled"], "the channel list still shows Slack as running")

# ---- unreachable vector store
from app.knowledge import stores
real_health = stores.ChromaStore.health
stores.ChromaStore.health = lambda self: (False, "connection refused")
res = storehealth.check_all(db)
row = [k for k in admin.get(P + "/kbs").json()["kbs"] if k["id"] == kb["id"]][0]
ok(res[kb["name"]] is False and row["health"]["ok"] is False, "an unreachable store shows its status on the knowledge base")
ok(any(a["kind"] == "store_unreachable" for a in alerts.active(db)), "and raises an alert for the Admin")
stores.ChromaStore.health = real_health
storehealth.check_all(db)
ok(not any(a["kind"] == "store_unreachable" for a in alerts.active(db)), "the alert clears when the store answers again")

# ---- retention
now = datetime.now(timezone.utc)
w.ask("amy@sysfore.com", "A recent question", kb_ids=[kb["id"]])
w.ask("amy@sysfore.com", "An old question", kb_ids=[kb["id"]])
with db.session() as s:
    convs = s.scalars(select(Conversation).order_by(Conversation.created_at)).all()
    old = [c for c in convs if any(m.content == "An old question" for m in s.scalars(select(Message).where(Message.conversation_id == c.id)))][0]
    old_id = old.id
    for m in s.scalars(select(Message).where(Message.conversation_id == old_id)):
        m.ts = now - timedelta(days=45)
    old.updated_at = now - timedelta(days=45)
before = w.store.recent_audit(500)
r = admin.post(P + "/conversations/retention/run", headers=ah)
with db.session() as s:
    left = {m.content for m in s.scalars(select(Message))}
ok(r.status_code == 200 and "An old question" not in left and "A recent question" in left, "retention removes a conversation older than the department's 30 days and keeps the recent one")
acts = w.store.recent_audit(500)
ok(any(e["action"] == "retention.run" for e in acts), "the retention run is in the audit log")

# ---- export: off by default, a reason is required, every export is audited
body = {"department": "HR", "format": "csv", "reason": "Quarterly compliance review"}
r = admin.post(P + "/conversations/export", headers=ah, json=body)
ok(r.status_code == 403, "export is refused while the setting is off (the default)")
admin.put(P + "/settings", headers=ah, json={"values": {"export_enabled": True}})
ok(admin.post(P + "/conversations/export", headers=ah, json={**body, "reason": "x"}).status_code == 422, "an export without a proper reason is refused")
r = admin.post(P + "/conversations/export", headers=ah, json=body)
rows = list(csv.DictReader(io.StringIO(r.text)))
ok(r.status_code == 200 and rows and all(x.get("department") == "HR" for x in rows), f"the export contains the department's messages ({len(rows)} rows)")
acts = w.store.recent_audit(500)
started = [e for e in acts if e["action"] == "conversation.export"]
ok(started and "Quarterly compliance review" in started[0]["detail"], "the export is audited with its reason")

# ---- nothing secret anywhere in the audit log
dump = json.dumps(acts, default=str)
ok(not any(x in dump for x in ("sk-main-111111", "sk-backup-222222", "xoxb-good", "xoxb-bad", "xapp-", "correct horse", "A recent question")),
   "the audit log holds no key, token, password or message text")
w.documents.shutdown()
