import os, sys, tempfile, time, json, re, hashlib, dataclasses
sys.path.insert(0, os.getcwd())
tmp = tempfile.mkdtemp()
from cryptography.fernet import Fernet
os.environ.update(ADMIN_DB_PATH=os.path.join(tmp, "admin.db"), CHAT_DIR=os.path.join(tmp, "chats"), CHROMA_PATH=os.path.join(tmp, "chroma"),
                  DATABASE_URL="", GUARDRAILS_ENABLED="false", APP_SECRET_KEY=Fernet.generate_key().decode(), CHAT_API_TOKEN="x")
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)
import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from app import config, retriever, usage
from app.admin import api as A, users, kbs_api, documents_api
from app.admin.auth import SessionSigner
from app.channels import get_store
from app.channels.manager import manager
from app.db import get_db, Secret, UsageRecord, ModelPrice
from app.knowledge import kbs, documents, versions, embeddings, chunkers
from app.providers.store import ProviderStore
from app.secretstore import generate_key

db = get_db()
default = kbs.ensure_default(db)
documents.settings = dataclasses.replace(config.settings, docs_dir=os.path.join(tmp, "files"))
store = get_store()

# ---- 1. the real local multilingual model
M = embeddings.LocalMultilingualEmbeddings
ok(M.available(), "the multilingual model files are present (downloaded earlier)")
m = embeddings.get("local-multilingual")
ok(m.test() == (True, "Works; vectors have 384 dimensions."), "test() reports the vector size")
en_q = "How many days of annual leave do employees get?"
passages = ["हर कर्मचारी को साल में 18 दिन की अर्जित छुट्टी मिलती है।", "The cafeteria serves lunch from noon every day.", "The VPN token is reset by the IT service desk."]
vq, vp = np.array(m.embed_query(en_q)), np.array(m.embed_documents(passages))
sims = vp @ vq
ok(abs(np.linalg.norm(vq) - 1) < 1e-3 and int(sims.argmax()) == 0, f"English question finds the Hindi leave sentence (similarities {np.round(sims, 2).tolist()})")
es_q = np.array(m.embed_query("¿Cuántos días de vacaciones tengo al año?"))
en_p = np.array(m.embed_documents(["Employees get 18 days of annual leave each year.", "Parking is free on Fridays.", "Reset your password from the portal."]))
ok(int((en_p @ es_q).argmax()) == 0, "a Spanish question finds the English leave sentence")

# ---- 2. API adapters against a fake service
calls = []


def fake_vector(text):
    v = np.zeros(64)
    for w in re.findall(r"\w+", text.lower()):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1
    n = np.linalg.norm(v)
    return (v / n if n else v).tolist()


MODE = {"fail": None, "rate_limit_times": 0}


def fake_post(url, headers, payload, timeout=60):
    calls.append({"url": url, "headers": headers, "payload": payload})
    if MODE["fail"] == 401:
        raise ValueError("The embedding service rejected the key (HTTP 401).")
    if MODE["rate_limit_times"] > 0:
        MODE["rate_limit_times"] -= 1
        raise embeddings.EmbeddingError("The embedding service is rate limiting this key (HTTP 429).")
    if "cohere" in url:
        return {"embeddings": {"float": [fake_vector(t) for t in payload["texts"]]}, "meta": {"billed_units": {"input_tokens": 7 * len(payload["texts"])}}}
    texts = payload["input"]
    return {"data": [{"index": i, "embedding": fake_vector(t)} for i, t in enumerate(texts)], "usage": {"total_tokens": 5 * len(texts)}}


embeddings._post = fake_post
ps = ProviderStore(db, store.secrets)
ps.set_approved(["openai"], "t")
pid = ps.create("openai", "My OpenAI", {"api_key": "sk-test-KEY-123456"}, [], "t")
with db.session() as s:
    s.add(ModelPrice(provider_type="openai", model="text-embedding-3-small", input_per_1k=0.02, output_per_1k=0.0))
kb = kbs.create_kb(db, "API docs", "", default["department_id"])
dept_name = kbs.department_name(db, default["department_id"])
oa = embeddings.build("openai", {"provider": "My OpenAI", "model": "text-embedding-3-small", "dimensions": 64}, kb, db)
vecs = oa.embed_documents(["alpha beta", "gamma"])
c = calls[-1]
ok(c["url"] == "https://api.openai.com/v1/embeddings" and c["headers"] == {"Authorization": "Bearer sk-test-KEY-123456"} and c["payload"]["model"] == "text-embedding-3-small" and c["payload"]["dimensions"] == 64 and len(vecs) == 2,
   "OpenAI: right URL, the key from LLM Providers (not copied), model and dimensions")
oa.embed_query("alpha")
with db.session() as s:
    rows = s.scalars(select(UsageRecord).where(UsageRecord.kind == "embedding").order_by(UsageRecord.id)).all()
ok(len(rows) == 2 and rows[0].purpose == "embed-doc" and rows[0].kb_id == kb["id"] and rows[0].input_tokens == 10 and rows[0].department == dept_name, "usage recorded: kind embedding, knowledge base, tokens from the service, department")
ok(rows[1].purpose == "embed-query" and rows[1].input_tokens == 5, "queries are recorded too")
ok(abs(rows[0].cost - 10 / 1000 * 0.02) < 1e-9, f"cost uses the hand-entered price ({rows[0].cost})")
oa_bad = embeddings.build("openai", {"provider": "Nobody", "model": "text-embedding-3-small"}, kb, db)
ok(oa_bad.test()[0] is False and "provider" in oa_bad.test()[1].lower(), "OpenAI without a usable provider key explains what to do")

az = embeddings.build("azure-openai", {"endpoint": "https://res.openai.azure.com/", "deployment": "emb1", "api_version": "2024-02-01"}, kb, db, extra_secrets={"api_key": "az-SECRET-999999"})
az.embed_documents(["x"])
c = calls[-1]
ok(c["url"] == "https://res.openai.azure.com/openai/deployments/emb1/embeddings?api-version=2024-02-01" and c["headers"] == {"api-key": "az-SECRET-999999"}, "Azure OpenAI: deployment URL, api-version and api-key header")
co = embeddings.build("cohere", {"model": "embed-multilingual-v3.0"}, kb, db, extra_secrets={"api_key": "co-SECRET-888888"})
co.embed_documents(["x"]); co.embed_query("x")
ok(calls[-2]["payload"]["input_type"] == "search_document" and calls[-1]["payload"]["input_type"] == "search_query" and calls[-1]["headers"]["Authorization"] == "Bearer co-SECRET-888888", "Cohere: documents and queries use the right input type")
MODE["fail"] = 401
okk, msg = co.test()
ok(not okk and "co-SECRET-888888" not in msg and "rejected the key" in msg, "a rejected key is reported without ever echoing the key")
MODE["fail"] = None

# retries: rate limits are retried with backoff, rejected keys are not
from app.knowledge.documents import add_in_batches
documents.RETRY_WAIT = (0.01, 0.05)
store_ = kbs.store_for({"collection": "kb_retry"}, embedder=co)
MODE["rate_limit_times"] = 2
n0 = len(calls)
add_in_batches(store_, ["a::0", "a::1"], ["alpha text", "beta text"], [{"doc_id": "a"}, {"doc_id": "a"}])
ok(store_.count() == 2 and len(calls) - n0 == 3, "a rate limit (429) is retried until it works (3 attempts)")
MODE["fail"] = 401; n0 = len(calls)
try:
    add_in_batches(store_, ["b::0"], ["gamma"], [{"doc_id": "b"}]); retried = None
except ValueError:
    retried = len(calls) - n0
ok(retried == 1, "a rejected key is not retried")
MODE["fail"] = None

# ---- 3. through the admin API: test before saving, secrets write-only, rebuild, switch, rollback
users.create_admin(db, "boss@sysfore.com", "correct horse battery", domain="sysfore.com")
users.create_user(db, "ed@sysfore.com", "Ed", "editor", default["department_id"])
ctx = A.AdminContext(db=db, store=store, manager=manager, signer=SessionSigner(generate_key()), domain="sysfore.com", allow_remote=True)
app = FastAPI(); app.include_router(A.router); app.include_router(kbs_api.router); app.include_router(documents_api.router)
app.dependency_overrides[A.get_ctx] = lambda: ctx
cl = TestClient(app, base_url="http://localhost")
h = {"X-CSRF-Token": cl.post("/admin/api/auth/login", json={"email": "boss@sysfore.com", "password": "correct horse battery"}).json()["csrf"]}
P = "/admin/api"
docs = {"leave.txt": "Employees get eighteen days of annual leave every year and unused leave carries forward.",
        "cafe.txt": "The cafeteria serves breakfast and lunch and the coffee machine is on the second floor.",
        "vpn.txt": "To reset the VPN token raise a ticket with the IT service desk."}
kb2 = cl.post(P + "/kbs", headers=h, json={"name": "Switch", "department_id": default["department_id"], "chunk_size": 300, "chunk_overlap": 30}).json()
ids = []
for name, text in docs.items():
    d = documents.add(db, kb2["id"], name, text.encode(), "boss@sysfore.com"); ids.append(d["id"])
end = time.time() + 120
while any(documents.get(db, i)["status"] != "Indexed" for i in ids) and time.time() < end:
    time.sleep(0.2)
opts = cl.get(P + "/kbs", headers=h).json()["embeddings"]
ok({o["name"] for o in opts} == {"chroma-default", "local-multilingual", "openai", "azure-openai", "cohere"}, "the screen is offered five embedding models")
oai = next(o for o in opts if o["name"] == "openai")
ok(next(f for f in oai["fields"] if f["name"] == "provider")["choices"] == ["My OpenAI"], "the OpenAI form lists the providers that already hold a key")
MODE["fail"] = 401
r = cl.put(f"{P}/kbs/{kb2['id']}", headers=h, json={"embedding": "cohere", "embedding_config": {"model": "embed-multilingual-v3.0"}, "embedding_secrets": {"api_key": "co-WRONG-KEY-0001"}})
with db.session() as s:
    nsec = len(s.scalars(select(Secret).where(Secret.owner_type == "kb-embedding")).all())
ok(r.status_code == 422 and "co-WRONG-KEY-0001" not in r.text and kbs.get_kb(db, kb2["id"])["embedding"] == "chroma-default" and nsec == 0, "a model that fails its test is refused and nothing (no key, no setting) is saved")
MODE["fail"] = None
r = cl.put(f"{P}/kbs/{kb2['id']}", headers=h, json={"embedding": "cohere", "embedding_config": {"model": "embed-multilingual-v3.0"}, "embedding_secrets": {"api_key": "co-GOOD-KEY-0002"}})
body = r.text
ok(r.status_code == 200 and "co-GOOD-KEY-0002" not in body and r.json()["embedding"] == "cohere" and r.json()["embedding_secrets_set"] == ["api_key"], "saved: the key is never returned, only that it is set")
with db.session() as s:
    sec = s.scalars(select(Secret).where(Secret.owner_type == "kb-embedding")).all()
ok(len(sec) == 1 and "co-GOOD-KEY-0002" not in sec[0].ciphertext and store.secrets.decrypt(sec[0].ciphertext) == "co-GOOD-KEY-0002", "the key is stored encrypted")
lst = json.dumps(cl.get(P + "/kbs", headers=h).json(), default=str)
ok("co-GOOD-KEY-0002" not in lst and "co-GOOD-KEY-0002" not in json.dumps(store.recent_audit(500), default=str), "the key is in no API response and no audit entry")
ok(versions.out_of_date(db, kbs.get_kb(db, kb2["id"])) and versions.active(db, kb2["id"])["embedding"] == "chroma-default", "the index is out of date; version 1 still uses the old model")
before = retriever.retrieve_scored("annual leave days", kb_ids=[kb2["id"]])
ok(before and before[0]["source"] == "leave.txt", "until the switch, questions still use the old model and find the answer")
n_cohere = sum(1 for c in calls if "cohere" in c["url"])
est = cl.get(f"{P}/kbs/{kb2['id']}/index/estimate", headers=h).json()
ok(est["model"] == "Cohere" and est["cost_usd"] is not None and est["cost_usd"] > 0 and "list price" in est["note"] and est["warning"], f"the rebuild estimate shows Cohere's cost ({est['cost_usd']} USD), a price note and a data warning")
cl.post(f"{P}/kbs/{kb2['id']}/index/rebuild", headers=h)
end = time.time() + 120
while versions.rebuilding(db, kb2["id"]) and time.time() < end:
    time.sleep(0.2)
act = versions.active(db, kb2["id"])
ok(act["version"] == 2 and act["embedding"] == "cohere" and act["embedding_config"] == {"model": "embed-multilingual-v3.0"}, "the rebuild built version 2 with Cohere and switched when it completed")
col = retriever.get_client().get_collection(act["collection"])
got = col.get(limit=1, include=["embeddings"])
ok(len(got["embeddings"][0]) == 64, "the new collection holds Cohere's vectors (64 in this fake)")
calls.clear()
after = retriever.retrieve_scored("annual leave days", kb_ids=[kb2["id"]])
ok(after and after[0]["source"] == "leave.txt" and calls and calls[-1]["payload"]["input_type"] == "search_query", "after the switch the question is embedded with the new model and finds the answer")
with db.session() as s:
    emb = s.scalars(select(UsageRecord).where(UsageRecord.kind == "embedding", UsageRecord.provider == "Cohere")).all()
ok(any(u.purpose == "embed-doc" and u.kb_id == kb2["id"] and u.input_tokens for u in emb) and any(u.purpose == "embed-query" for u in emb), "the rebuild and the question were both recorded as embedding usage for this knowledge base")
# rollback
vers = versions.list_versions(db, kb2["id"]); v1 = next(v for v in vers if v["version"] == 1)
cl.post(f"{P}/kbs/{kb2['id']}/index/rollback", headers=h, json={"version_id": v1["id"]})
calls.clear()
back = retriever.retrieve_scored("annual leave days", kb_ids=[kb2["id"]])
ok(back and back[0]["source"] == "leave.txt" and not calls, "after a rollback questions use the old model again (no call to the new one)")
# semantic chunking uses the KB's embedding model
cl.put(f"{P}/kbs/{kb2['id']}", headers=h, json={"chunk_strategy": "semantic"})
calls.clear()
sem = chunkers.build("semantic", 300, 30, embeddings.build("cohere", {"model": "embed-multilingual-v3.0"}, kb2, db))
sem.chunk(__import__("pathlib").Path(tmp) / "x.txt") if False else None
f = __import__("pathlib").Path(tmp) / "s.txt"; f.write_text("Alpha beta gamma. Delta epsilon zeta. Eta theta iota.", encoding="utf-8")
sem.chunk(f)
ok(any("cohere" in c["url"] for c in calls), "semantic chunking embeds sentences with the knowledge base's own model")

# ---- 4. real local multilingual through the whole flow
kb3 = cl.post(P + "/kbs", headers=h, json={"name": "Multi", "department_id": default["department_id"]}).json()
for name, text in {"leave.txt": "Employees get eighteen days of annual leave every year.", "cafe.txt": "The cafeteria serves lunch from noon.", "vpn.txt": "Reset the VPN token with the IT service desk."}.items():
    d = documents.add(db, kb3["id"], name, text.encode(), "boss@sysfore.com")
end = time.time() + 120
while any(x["status"] != "Indexed" for x in documents.list_for_kb(db, kb3["id"])) and time.time() < end:
    time.sleep(0.2)
r = cl.put(f"{P}/kbs/{kb3['id']}", headers=h, json={"embedding": "local-multilingual"})
ok(r.status_code == 200, "the local multilingual model passes its test and is saved")
cl.post(f"{P}/kbs/{kb3['id']}/index/rebuild", headers=h)
end = time.time() + 180
while versions.rebuilding(db, kb3["id"]) and time.time() < end:
    time.sleep(0.2)
hindi = retriever.retrieve_scored("सालाना छुट्टी कितने दिन की होती है", kb_ids=[kb3["id"]])
ok(versions.active(db, kb3["id"])["embedding"] == "local-multilingual" and hindi and hindi[0]["source"] == "leave.txt", f"a Hindi question finds the English leave document (top: {hindi[0]['source'] if hindi else None}, score {hindi[0]['score'] if hindi else None})")
# download rules
orig = config.settings
embeddings.LocalMultilingualEmbeddings._session = None
embeddings.LocalMultilingualEmbeddings.folder = staticmethod(lambda: __import__("pathlib").Path(tmp) / "nomodel")
rr = cl.put(f"{P}/kbs/{kb3['id']}", headers=h, json={"embedding": "local-multilingual"})
ok(rr.status_code == 422 and "downloaded" in rr.text, "if the model files are missing the Test explains that it must be downloaded first")
ed = TestClient(app, base_url="http://localhost")
eh = {"X-CSRF-Token": ed.post(P + "/auth/login", json={"email": "ed@sysfore.com", "password": "x"}).json().get("csrf", "")} if False else None
documents.shutdown()
