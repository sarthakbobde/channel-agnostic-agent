"""Release 2 acceptance: the three features together, a suggestion click through each chat app's real handler, the
adversarial permission checks for glossary expansion, suggestions and traces on every store adapter, and a small load and
overhead measurement. Models are stand-ins; the stores, retrieval, permissions and settings are real."""
import statistics
import threading
import time
import types

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app import chat_service, glossary, global_settings, identity, language, prompts, rag, scoped_settings, suggestions, trace
from app.admin import users
from app.api import app
from app.channels import core, gchat, slack, teams
from app.channels.base import Envelope
from app.db import AnswerTrace, Department, Glossary, GlossaryTerm, GlossaryVersion, ScopedSetting, Suggestion, get_db
from app.knowledge import documents, embeddings, kbs, storeadapters
from tests.test_baseline import FakeMessage, FakeModel
from tests.test_platforms import GOOD, PROJECT, bearer, chat_app, keys, teams_app, token, wait_for  # noqa: F401 (fixtures)
from tests.test_store_contract import PG_URL, FakeAzure

P = "/admin/api"
PW = "a good password 1"
KEY = "y" * 43 + "="


def sign_in(email):
    c = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000), raise_server_exceptions=False)
    r = c.post(P + "/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    return c, {"X-CSRF-Token": r.json()["csrf"]}


def person(db, email, role, department_id):
    u = users.create_user(db, email, email.split("@")[0], role, department_id)
    token_, _ = users.issue_set_password_link(db, u["id"], "setup")
    users.redeem_set_password_link(db, token_, PW)
    return u


@pytest.fixture(scope="module")
def world():
    db = get_db()
    kbs.ensure_default(db)
    prompts.seed(db)
    w = types.SimpleNamespace(db=db)
    with db.session() as s:
        s.add(Department(name="RaHR", retention_days=30, banned_topics="", is_default=False))
        s.add(Department(name="RaFin", retention_days=30, banned_topics="", is_default=False))
    with db.session() as s:
        w.hr = s.scalar(select(Department.id).where(Department.name == "RaHR"))
        w.fin = s.scalar(select(Department.id).where(Department.name == "RaFin"))
    users.create_admin(db, "ra-boss@sysfore.com", PW, domain="sysfore.com")
    w.asker = person(db, "ra-asker@sysfore.com", "viewer", w.hr)
    w.insider = person(db, "ra-insider@sysfore.com", "viewer", w.hr)
    person(db, "ra-fin-ed@sysfore.com", "editor", w.fin)
    from app.db import DepartmentEditor
    with db.session() as s:
        s.add(DepartmentEditor(department_id=w.fin, user_id=s.scalar(select(users.User.id).where(users.User.email == "ra-fin-ed@sysfore.com"))))
    w.admin, w.ah = sign_in("ra-boss@sysfore.com")
    w.fin_ed, w.fh = sign_in("ra-fin-ed@sysfore.com")
    w.kb = w.admin.post(P + "/kbs", headers=w.ah, json={"name": "Ra HR", "department_id": w.hr}).json()
    w.e5 = embeddings.LocalMultilingualEmbeddings.available()
    w.kb2 = w.admin.post(P + "/kbs", headers=w.ah, json={"name": "Ra HR multilingual", "department_id": w.hr,
                                                         **({"embedding": "local-multilingual"} if w.e5 else {})}).json()

    def upload(kb, name, text, **form):
        r = w.admin.post(f"{P}/kbs/{kb['id']}/documents", headers=w.ah, files={"file": (name, text.encode())}, data=form)
        assert r.status_code == 201, r.text
        end = time.time() + 120
        while time.time() < end and documents.get(db, r.json()["id"])["status"] not in ("Indexed", "Failed"):
            time.sleep(0.2)
        d = documents.get(db, r.json()["id"])
        assert d["status"] == "Indexed", d["error"]
        return d
    upload(w.kb, "leave.txt", "Every employee gets 20 days of paid time off each year. Unused paid time off can be carried over once.")
    upload(w.kb, "parking.txt", "Parking in the north lot is free for staff on Fridays.")
    upload(w.kb, "secret.txt", "Paid time off for board members is 40 days and the board bonus pool is forty percent.",
           visibility="people", user_ids=str(w.insider["id"]))
    upload(w.kb2, "laptops.txt", "Laptops are refreshed every three years by the IT desk. Paid time off requests go through the HR portal.")
    yield w
    documents.shutdown()


@pytest.fixture(autouse=True)
def setup(world, monkeypatch):
    monkeypatch.setattr(suggestions, "env", types.SimpleNamespace(secret_key=KEY))
    for kb in (world.kb, world.kb2):
        scoped_settings.put("kb", kb["id"], {"suggestions_enabled": True, "glossary_mode": "query", "score_threshold": 0.0}, "t", world.db)
    for ch in ("web", "slack", "teams", "gchat"):
        scoped_settings.put("channel", ch, {"suggestions_enabled": True}, "t", world.db)
    glossary.save_draft("department", world.hr, [{"canonical": "paid time off", "aliases": ["PTO"], "kind": "acronym"}], "t")
    glossary.publish(glossary.view("department", world.hr)["draft"]["id"], "t")
    yield
    with get_db().session() as s:
        for model in (ScopedSetting, Suggestion, GlossaryTerm, GlossaryVersion, Glossary):
            s.execute(delete(model))
    scoped_settings.invalidate()
    glossary.invalidate()
    suggestions._cache.clear()


def models(monkeypatch, answer, candidates=(), query=None):
    router = FakeModel(reply='{"search": true, "query": "%s"}' % (query or "PTO"))
    text = answer + ("\n<<<SUGGESTIONS>>>\n" + "\n".join(candidates) + "\n<<<END>>>" if candidates else "")
    answerer = FakeModel(parts=[text])
    monkeypatch.setattr(chat_service, "get_chat_model", lambda: router)
    monkeypatch.setattr(rag, "get_chat_model", lambda: answerer)
    return router, answerer


def ask(world, question, conv, email="ra-asker@sysfore.com", kb_ids=None):
    tok = identity.bind(identity.resolve(email, world.db))
    try:
        return chat_service.chat(question, conv, kb_ids=kb_ids or [world.kb["id"], world.kb2["id"]], asker="web:" + email)
    finally:
        identity.unbind(tok)


# ---- 1. combined scenario -----------------------------------------------------------------------------------------------------
def test_hindi_question_with_an_english_acronym_across_two_kbs_with_suggestions(world, monkeypatch):
    if not world.e5:
        pytest.skip("the multilingual model is not downloaded here, so the second KB cannot use a different embedder")
    _, answerer = models(monkeypatch, "हर साल 20 दिन का PTO मिलता है [1]।",
                         ["क्या बची हुई छुट्टी अगले साल ले जा सकते हैं?", "Is parking free on Fridays?"], query="मेरा PTO कितने दिन का है?")
    r = ask(world, "मेरा PTO कितने दिन का है?", "web-1-ra-combo")
    assert r["language"] == "hi" and r["found"] and "Write your whole reply in Hindi" in answerer.calls[0][1][0].content
    with get_db().session() as s:
        t = s.get(AnswerTrace, r["answer_id"])
    assert {k for k, v in t.glossary.items() if v["applied"]} == {str(world.kb["id"]), str(world.kb2["id"])}   # the department's term, in both KBs
    assert {x["kb_id"] for x in t.retrieval} == {world.kb["id"], world.kb2["id"]}
    assert {x["kb_id"] for x in t.hits} == {world.kb["id"], world.kb2["id"]}               # both embedders searched
    assert "secret.txt" not in {s_["source"] for s_ in r["sources"]}
    assert r["suggestions"] and all(s_["payload"] for s_ in r["suggestions"])
    assert answerer.calls[0][1][-1].content == "मेरा PTO कितने दिन का है?"                   # the question as written, never expanded


# ---- 2. a suggestion click through each chat app's own handler ------------------------------------------------------------------
RESULT = {"answer": "Every three years [1].", "sources": [], "language": "en", "suggestions": []}


def test_slack_click(world, monkeypatch):
    seen, said = [], []
    monkeypatch.setattr(slack, "handle_action", lambda env, payload: seen.append((env, payload)) or RESULT)
    ad = slack.SlackAdapter.__new__(slack.SlackAdapter)
    ad.cfg, ad.allowed, ad.show_sources, ad.presenter = {"kb_ids": [world.kb["id"]]}, set(), True, slack.SlackPresenter()
    ad._email_for = lambda user: "ra-asker@sysfore.com"
    body = {"actions": [{"action_id": "caa_suggest_0", "value": "P.sig"}], "user": {"id": "U1", "team_id": "T1"},
            "channel": {"id": "C1"}, "team": {"id": "T1"}, "message": {"ts": "200.2", "thread_ts": "171.1"}}
    ad._on_action(lambda: None, body, lambda **kw: said.append(kw))
    env, payload = seen[0]
    assert payload == "P.sig" and env.conv_id == "slack-T1-C1-171.1" and env.email == "ra-asker@sysfore.com"
    assert said[0]["thread_ts"] == "171.1" and said[0]["text"] == "Every three years [1]."


def test_teams_click(teams_app, world, monkeypatch):
    c, ad, out = teams_app
    seen = []
    monkeypatch.setattr(teams, "handle_action", lambda env, payload: seen.append((env, payload)) or RESULT)
    from tests.test_platforms import fixture
    activity = fixture("teams_message.json")
    activity.pop("text", None)
    activity["value"] = {"caa_action": "P.sig"}
    assert c.post("/channels/teams/messages", json=activity, headers=GOOD).status_code == 202
    assert wait_for(lambda: out["replies"])
    env, payload = seen[0]
    assert payload == "P.sig" and env.conv_id.startswith("teams-") and out["replies"][0] == "Every three years [1]."


def test_google_chat_click_in_both_formats(chat_app, keys, world, monkeypatch):
    m, c = chat_app
    m.apply("gchat", {"auth": "project_number", "audience": PROJECT, "kb_ids": [world.kb["id"]]})
    seen = []
    monkeypatch.setattr(gchat, "handle_action", lambda env, payload: seen.append((env, payload)) or {**RESULT, "suggestions": [{"text": "Next?", "payload": "Q.sig"}]})
    h = bearer(token(keys["google"][0], iss=gchat.CHAT_ISSUER, aud=PROJECT))
    classic = {"type": "CARD_CLICKED", "action": {"actionMethodName": "caa_suggest", "parameters": [{"key": "payload", "value": "P.sig"}]},
               "space": {"name": "spaces/A"}, "message": {"thread": {"name": "spaces/A/threads/T"}}, "user": {"name": "users/1", "email": "ra-asker@sysfore.com"}}
    r = c.post("/channels/gchat/events", json=classic, headers=h).json()
    assert r["actionResponse"] == {"type": "NEW_MESSAGE"} and r["text"] == "Every three years [1]." and r["cardsV2"]
    addon = {"commonEventObject": {"invokedFunction": "caa_suggest", "parameters": {"payload": "P.sig"}},
             "chat": {"user": {"name": "users/1"}, "buttonClickedPayload": {"space": {"name": "spaces/A"}, "message": {"thread": {"name": "spaces/A/threads/T"}}}}}
    r = c.post("/channels/gchat/events", json=addon, headers=h).json()
    assert r["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]["text"] == "Every three years [1]."
    assert [e.conv_id for e, _ in seen] == ["gchat-g-spaces/A-spaces/A/threads/T"] * 2


# ---- 3. adversarial permissions on every store adapter ---------------------------------------------------------------------------
def _copy_into(store, kb):
    """The KB's indexed passages, written as they are into another store adapter (text, metadata and access)."""
    src = kbs.store_for(kb)
    for ids, _ in src.iter_metadata():
        got = src.get(ids)
        store.add([g["id"] for g in got], [g["text"] for g in got], [g["metadata"] for g in got])


@pytest.fixture(params=["chroma", "azure", "pgvector"])
def adapter(request, world, monkeypatch):
    if request.param == "chroma":
        yield "chroma"
        return
    emb = embeddings.get(embeddings.DEFAULT)
    if request.param == "azure":
        monkeypatch.setattr(storeadapters, "_http", FakeAzure())
        store = storeadapters.AzureSearchStore("ra" + str(world.kb["id"]), {"endpoint": "https://svc.search.windows.net", "index_name": "caa"},
                                               {"api_key": FakeAzure.KEY}, emb)
    else:
        if not PG_URL:
            pytest.skip("set TEST_PGVECTOR_URL to run against PostgreSQL + pgvector")
        store = storeadapters.PgVectorStore("ra" + str(world.kb["id"]), {"schema": "public"}, {"dsn": PG_URL}, emb)
    _copy_into(store, world.kb)
    real = kbs.store_for
    monkeypatch.setattr(kbs, "store_for", lambda kb: store if kb.get("id") == world.kb["id"] else real(kb))
    yield request.param
    store.delete()


def test_glossary_expansion_never_reaches_a_restricted_document(world, adapter, monkeypatch):
    models(monkeypatch, "20 days [1].", query="PTO for board members")
    r = ask(world, "PTO for board members?", f"web-1-ra-adv-g-{adapter}", kb_ids=[world.kb["id"]])
    assert "secret.txt" not in {s_["source"] for s_ in r["sources"]}
    with get_db().session() as s:
        t = s.get(AnswerTrace, r["answer_id"])
        assert t.glossary[str(world.kb["id"])]["applied"]                                  # expansion did run
    models(monkeypatch, "40 days [1].", query="PTO for board members")
    r = ask(world, "PTO for board members?", f"web-1-ra-adv-g2-{adapter}", email="ra-insider@sysfore.com", kb_ids=[world.kb["id"]])
    assert "secret.txt" in {s_["source"] for s_ in r["sources"]}                              # the person allowed still gets it


def test_suggestions_never_leak_a_restricted_document(world, adapter, monkeypatch):
    scoped_settings.put("kb", world.kb["id"], {"score_threshold": 0.35}, "t", world.db)    # a real threshold: support must be relevant
    models(monkeypatch, "20 days [1].", ["How big is the board bonus pool?"])
    r = ask(world, "PTO?", f"web-1-ra-adv-s-{adapter}", kb_ids=[world.kb["id"]])
    assert r["suggestions"] == []
    with get_db().session() as s:
        assert s.scalar(select(Suggestion.outcome).where(Suggestion.conversation_id == f"web-1-ra-adv-s-{adapter}")) == "Not answerable"


def test_traces_never_show_a_restricted_passage(world, adapter, monkeypatch):
    models(monkeypatch, "40 days [1].", query="PTO for board members")
    r = ask(world, "PTO for board members?", f"web-1-ra-adv-t-{adapter}", email="ra-insider@sysfore.com", kb_ids=[world.kb["id"]])
    assert world.fin_ed.get(f"{P}/traces/{r['answer_id']}", headers=world.fh).status_code == 404   # another department: nothing
    t = world.admin.get(f"{P}/traces/{r['answer_id']}", headers=world.ah).json()
    # Admins manage every knowledge base, so they may read every document in it (access.manages, unchanged since Phase 4a)
    assert any(h["status"] == "visible" and "board" in (h.get("text") or "") for h in t["hits"])
    models(monkeypatch, "20 days [1].", query="PTO for board members")
    r = ask(world, "PTO for board members?", f"web-1-ra-adv-t2-{adapter}", kb_ids=[world.kb["id"]])
    t = world.admin.get(f"{P}/traces/{r['answer_id']}", headers=world.ah).json()
    assert not any("board" in (h.get("text") or "") for h in t["hits"])                  # the asker never got it, so it is not in their trace


# ---- 4. load and overhead --------------------------------------------------------------------------------------------------------
def test_concurrent_questions_all_answer_with_one_trace_each(world, monkeypatch):
    models(monkeypatch, "20 days [1].", ["Is parking free on Fridays?"])
    results, errors = [], []

    def one(i):
        try:
            results.append(ask(world, "How much PTO do I get?", f"web-1-ra-load-{i}"))
        except Exception as e:
            errors.append(e)
    threads = [threading.Thread(target=one, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join(120) for t in threads]
    assert not errors and len(results) == 12 and all(r["found"] for r in results)
    with get_db().session() as s:
        assert s.scalar(select(func.count()).select_from(AnswerTrace).where(AnswerTrace.conversation_id.like("web-1-ra-load-%"))) == 12


def test_overhead_with_all_three_features_on(world, monkeypatch):
    """Time spent in this server per question (models are instant stand-ins), with glossary, languages and suggestions on."""
    models(monkeypatch, "20 days [1].", ["Is parking free on Fridays?", "Can unused paid time off be carried over?", "Where is the HR portal?"])
    times = []
    for i in range(10):
        t = time.perf_counter()
        ask(world, "How much PTO do I get?", f"web-1-ra-time-{i}")
        times.append(time.perf_counter() - t)
    p95 = sorted(times)[int(len(times) * 0.95) - 1]
    print(f"overhead per question: median {statistics.median(times):.2f}s, p95 {p95:.2f}s")
    assert p95 < 5.0
