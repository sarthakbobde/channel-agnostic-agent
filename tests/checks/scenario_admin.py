"""Scenario: an Admin sets the whole system up through the admin API, then a Viewer asks a question and the Admin reads the cost.
providers (two) -> preferred and fallback -> departments and people -> knowledge base (heading chunks, built-in embedding, Chroma)
-> upload -> access -> bind to Slack -> base prompt and guardrails -> a question -> Usage and cost and Reports -> audit."""
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _scenario import boot

w = boot()
ok, db, P = w.ok, w.db, "/admin/api"
w.users.create_admin(db, "boss@sysfore.com", "correct horse battery", domain="sysfore.com")
admin, ah = w.login("boss@sysfore.com", "correct horse battery")

# ---- two providers; keys are write-only
r = admin.put(P + "/providers/approval", headers=ah, json={"approved": ["openai", "anthropic"]})
ok(r.status_code == 200, "the Admin approves two provider types")
k1, k2 = "sk-OPENAI-SECRET-123456", "sk-ant-SECRET-654321"
p1 = admin.post(P + "/providers", headers=ah, json={"type": "openai", "name": "OpenAI main", "values": {"api_key": k1}})
p2 = admin.post(P + "/providers", headers=ah, json={"type": "anthropic", "name": "Claude backup", "values": {"api_key": k2}})
ok(p1.status_code == 201 and p2.status_code == 201 and k1 not in p1.text and k2 not in p2.text, "both providers are saved after a successful test, and the keys are never returned")
ok(k1 not in admin.get(P + "/providers").text, "the provider list does not contain a key either")
r = admin.put(P + "/model-settings", headers=ah, json={"preferred": {"provider_id": p1.json()["id"], "model": "model-a"},
                                                         "fallback": {"provider_id": p2.json()["id"], "model": "model-b"}, "request_timeout": 30})
ms = admin.get(P + "/model-settings").json()
ok(r.status_code == 200 and ms["preferred"]["model"] == "model-a" and ms["fallback"]["model"] == "model-b" and ms["request_timeout"] == 30, "preferred and fallback models are set")

# ---- departments and people
hr = admin.post(P + "/departments", headers=ah, json={"name": "HR", "retention_days": 90, "banned_topics": "salary"}).json()["id"]
admin.post(P + "/departments", headers=ah, json={"name": "IT", "retention_days": 60, "banned_topics": ""})
r = admin.post(P + "/users", headers=ah, json={"email": "ed@sysfore.com", "name": "Ed", "role": "editor", "department_id": hr})
ok(r.status_code == 201 and r.json()["link"]["target"] == "admin", "an Editor is added and gets a one-time set-password link")
anon = w.client()
ok(anon.post(P + "/auth/set-password", json={"token": r.json()["link"]["token"], "password": "editor password 1"}).status_code == 200, "the Editor chooses a password with the link")
r = admin.post(P + "/users", headers=ah, json={"email": "amy@sysfore.com", "name": "Amy", "role": "viewer", "department_id": hr})
ok(r.status_code == 201 and r.json()["link"]["target"] == "web", "a Viewer is added with a link for the Web UI")

# ---- knowledge base: heading-aware chunks, built-in embedding model, Chroma
kb = admin.post(P + "/kbs", headers=ah, json={"name": "HR policies", "department_id": hr, "chunk_strategy": "by_heading"})
ok(kb.status_code == 201 and kb.json()["chunk_strategy"] == "by_heading", "a knowledge base is created with the heading chunking strategy")
kb_id = kb.json()["id"]
filler = " Leave is requested in the HR portal and approved by the line manager, who answers within two working days." * 4
md = ("# Leave policy\n\n## Annual leave\n\nEvery employee gets 18 days of annual leave each year." + filler +
      "\n\n## Sick leave\n\nSick leave is 12 days per year." + filler + "\n").encode()
up = admin.post(f"{P}/kbs/{kb_id}/documents", headers=ah, files={"file": ("leave.md", md)})
d = w.wait_doc(up.json()["id"])
ok(up.status_code == 201 and d["status"] == "Indexed" and d["chunks"] >= 2, f"the document is uploaded, split by heading and indexed ({d['chunks']} chunks)")
st = w.kbs.store_for(w.kbs.get_kb(db, kb_id))
paths = {m.get("heading_path") for _, ms in st.iter_metadata() for m in ms}
ok(any("Annual leave" in (p or "") for p in paths), "chunks carry their heading path")
r = admin.put(f"{P}/documents/{up.json()['id']}/access", headers=ah, json={"visibility": "everyone"})
ok(r.status_code == 200, "document access is set (everyone)")

# ---- bind to Slack; secrets stay secret
r = admin.put(P + "/channels/slack", headers=ah, json={"enabled": True, "values": {"bot_token": "xoxb-SLACK-SECRET-1", "app_token": "xapp-SLACK-SECRET-2", "kb_ids": [kb_id]}})
ok(r.status_code == 200 and "SLACK-SECRET" not in r.text and w.manager.get("slack") is not None, "Slack is switched on, bound to the knowledge base, and its tokens are not returned")
ch = admin.get(P + "/channels/slack").json()
ok("SLACK-SECRET" not in json.dumps(ch), "the channel view never shows the tokens")

# ---- base prompt and guardrails
base = admin.get(P + "/prompts/versions", params={"scope": "base-answer"}).json()
vs = base["versions"] if isinstance(base, dict) else base
cur = [v for v in vs if v["status"] == "Published"]
text = (cur[0]["text"] if cur and cur[0].get("text") else "You answer from the documents.") + "\nAlways end with: Thanks for asking."
dr = admin.post(P + "/prompts/drafts", headers=ah, json={"scope": "base-answer", "text": text})
pb = admin.post(P + f"/prompts/versions/{dr.json()['id']}/publish", headers=ah)
ok(dr.status_code == 201 and pb.status_code == 200, "a new base prompt is saved as a draft and published")
r = admin.put(P + "/guardrails", headers=ah, json={"values": {"max_input_tokens": 400}})
ok(r.status_code == 200, "guardrail settings are updated")

# ---- a price is typed in by hand (it applies to calls made from then on)
admin.put(P + "/prices", headers=ah, json={"provider_type": "openai", "model": "model-a", "input_per_1k": 0.5, "output_per_1k": 1.5})

# ---- a Viewer asks in Slack; the preferred model answers from the document
res = w.ask("amy@sysfore.com", "How many days of annual leave do I get?", kb_ids=[kb_id])
ok("18 days" in res["answer"] and res["sources"] and res["sources"][0]["source"] == "leave.md", "the answer cites leave.md")
ok(w.models["openai"].calls >= 2 and w.models["anthropic"].calls == 0, "the preferred model did the work; the fallback was not used")
seen = " ".join(w.models["openai"].seen)
ok("18 days of annual leave" in seen and "Thanks for asking" in seen, "the model was given the retrieved passage and the new base prompt")

# ---- Usage and cost, then Reports
u = admin.get(P + "/usage", params={"group": "provider"}).json()
rows = {x["label"]: x for x in u["rows"]}
ok("OpenAI main" in rows and rows["OpenAI main"]["calls"] >= 2 and rows["OpenAI main"]["cost"] > 0, "Usage and cost shows the preferred provider's calls with a cost")
ok(u["totals"]["calls"] >= 2 and "Claude backup" not in rows, "the fallback provider has no calls in the list")
k = admin.get(P + "/reports/knowledge").json()["rows"]
mine = [x for x in k if x["id"] == kb_id][0]
ok(mine["answered"] == 1 and mine["not_found"] == 0 and mine["by_status"].get("Indexed") == 1, "the knowledge-base report shows one answered question and one indexed document")
c = {x["type"]: x for x in admin.get(P + "/reports/channels").json()["rows"]}
ok(c["slack"]["running"] and c["slack"]["conversations"] == 1, "the channel report shows Slack running with its conversation")

# ---- the audit log has every step and no secret
rows = admin.get(P + "/audit/search", params={"limit": 200}).json()
acts = {x["action"] for x in rows["rows"]}
need = {"login", "providers.approval", "provider.create", "user.add", "user.link", "department.add", "kb.create", "document.upload",
        "document.access", "channel.create", "prompt.save", "prompt.publish", "guardrails.update", "price.set"}
ok(need <= acts, f"every configuration step is in the audit log (missing: {sorted(need - acts)})")
dump = json.dumps(rows)
ok(not any(s in dump for s in (k1, k2, "SLACK-SECRET", "editor password", "correct horse")), "no secret, token or password appears anywhere in the audit log")
from app.db import Secret
from sqlalchemy import select
with db.session() as s:
    cipher = " ".join(x.ciphertext for x in s.scalars(select(Secret)))
ok(k1 not in cipher and "SLACK-SECRET" not in cipher, "stored credentials are encrypted in the database")
w.documents.shutdown()
