"""Release 2, Phase 3: glossaries (D22), against the real application with real documents and the real Chroma store."""
import time
import types

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import chat_service, glossary, global_settings, identity, prompts, rag, retriever, scoped_settings
from app.admin import users
from app.api import app
from app.db import AnswerTrace, AuditLog, Department, DepartmentEditor, Glossary, GlossaryTerm, GlossaryVersion, ScopedSetting, get_db
from app.knowledge import access, documents, kbs
from tests.test_baseline import FakeModel

P = "/admin/api"
PW = "a good password 1"
PTO = [{"canonical": "paid time off", "aliases": ["PTO"], "kind": "acronym"}]


def sign_in(email):
    c = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000), raise_server_exceptions=False)
    r = c.post(P + "/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    return c, {"X-CSRF-Token": r.json()["csrf"]}


def person(db, email, role, department_id):
    u = users.create_user(db, email, email.split("@")[0], role, department_id)
    token, _ = users.issue_set_password_link(db, u["id"], "setup")
    users.redeem_set_password_link(db, token, PW)
    return u


@pytest.fixture(scope="module")
def world():
    db = get_db()
    kbs.ensure_default(db)
    prompts.seed(db)
    w = types.SimpleNamespace(db=db)
    with db.session() as s:
        s.add(Department(name="GlHR", retention_days=30, banned_topics="", is_default=False))
        s.add(Department(name="GlIT", retention_days=30, banned_topics="", is_default=False))
    with db.session() as s:
        w.hr = s.scalar(select(Department.id).where(Department.name == "GlHR"))
        w.it = s.scalar(select(Department.id).where(Department.name == "GlIT"))
    users.create_admin(db, "gl-boss@sysfore.com", PW, domain="sysfore.com")
    ed = person(db, "gl-ed-hr@sysfore.com", "editor", w.hr)
    person(db, "gl-asker@sysfore.com", "viewer", w.hr)
    w.outsider = person(db, "gl-other@sysfore.com", "viewer", w.hr)
    with db.session() as s:
        s.add(DepartmentEditor(department_id=w.hr, user_id=ed["id"]))
    w.admin, w.ah = sign_in("gl-boss@sysfore.com")
    w.edhr, w.eh = sign_in("gl-ed-hr@sysfore.com")
    w.kb = w.admin.post(P + "/kbs", headers=w.ah, json={"name": "Gl HR", "department_id": w.hr}).json()
    w.kb_it = w.admin.post(P + "/kbs", headers=w.ah, json={"name": "Gl IT", "department_id": w.it}).json()

    def upload(kb, name, text, **form):
        r = w.admin.post(f"{P}/kbs/{kb['id']}/documents", headers=w.ah, files={"file": (name, text.encode())}, data=form)
        assert r.status_code == 201, r.text
        end = time.time() + 90
        while time.time() < end and documents.get(db, r.json()["id"])["status"] not in ("Indexed", "Failed"):
            time.sleep(0.2)
        return documents.get(db, r.json()["id"])
    w.upload = upload
    w.pto = upload(w.kb, "leave.txt", "Every employee gets 20 days of paid time off each year, approved by the line manager.")
    upload(w.kb, "parking.txt", "Parking in the north lot is free for staff on Fridays.")
    upload(w.kb, "laptops.txt", "Laptops are refreshed every three years by the IT desk.")
    upload(w.kb_it, "it.txt", "The VPN token is reset from the self-service portal.")
    yield w
    documents.shutdown()


@pytest.fixture(autouse=True)
def clean(world):
    yield
    with get_db().session() as s:
        s.execute(delete(ScopedSetting))
        s.execute(delete(GlossaryTerm))
        s.execute(delete(GlossaryVersion))
        s.execute(delete(Glossary))
    scoped_settings.invalidate()
    glossary.invalidate()


def publish(scope_type, scope_id, terms, actor="t"):
    glossary.save_draft(scope_type, scope_id, terms, actor)
    draft = glossary.view(scope_type, scope_id)["draft"]
    return glossary.publish(draft["id"], actor)[0]


def turn_on(world, mode="query", kb=None):
    scoped_settings.put("kb", (kb or world.kb)["id"], {"glossary_mode": mode}, "t", world.db)


def ask(world, monkeypatch, question, conv, email="gl-asker@sysfore.com", kb=None):
    monkeypatch.setattr(chat_service, "get_chat_model", lambda: FakeModel(reply='{"search": true, "query": "%s"}' % question))
    answerer = FakeModel(parts=["You get 20 days of paid time off [1]."])
    monkeypatch.setattr(rag, "get_chat_model", lambda: answerer)
    token = identity.bind(identity.resolve(email, world.db))
    try:
        return chat_service.chat(question, conv, kb_ids=[(kb or world.kb)["id"]]), answerer
    finally:
        identity.unbind(token)


def searched_texts(monkeypatch):
    seen = []
    real = kbs.store_for

    def spy(kb):
        store = real(kb)
        original = store.search

        def search(q, k, flt=None):
            seen.append(q)
            return original(q, k, flt)
        store.search = search
        return store
    monkeypatch.setattr(kbs, "store_for", spy)
    return seen


# ---- the resolver -----------------------------------------------------------------------------------------------------------
def test_matching_is_on_whole_words_including_multi_word_terms():
    terms = glossary.clean_terms([{"canonical": "paid time off", "aliases": ["PTO", "earned leave"], "kind": "acronym"},
                                  {"canonical": "work from home", "aliases": ["WFH"]}])
    e = glossary.expand_with("How many PTO days?", terms, max_terms=5, max_aliases=3)
    assert e.search_text == "How many PTO days? paid time off earned leave" and e.applied[0]["matched"] == "PTO"
    assert glossary.expand_with("PTOX and WFHX", terms, max_terms=5, max_aliases=3).applied == []      # no partial words
    assert glossary.expand_with("Can I work from home?", terms, max_terms=5, max_aliases=3).search_text == "Can I work from home? WFH"
    assert glossary.expand_with("my earned leaves", terms, max_terms=5, max_aliases=3).applied[0]["matched"] == "earned leave"   # plurals fold


def test_case_language_kind_and_caps():
    terms = glossary.clean_terms([
        {"canonical": "Information Technology", "aliases": ["IT"], "case_sensitive": True},
        {"canonical": "छुट्टी", "aliases": ["अवकाश"], "language": "hi"},
        {"canonical": "line manager", "aliases": ["boss", "supervisor"], "kind": "preferred"}])
    assert glossary.expand_with("is it done", terms, max_terms=5, max_aliases=3).applied == []            # "it" is not "IT"
    assert glossary.expand_with("ask IT", terms, max_terms=5, max_aliases=3).applied
    assert glossary.expand_with("मेरी छुट्टी", terms, "hi", max_terms=5, max_aliases=3).search_text.endswith("अवकाश")
    assert glossary.expand_with("मेरी छुट्टी", terms, "mr", max_terms=5, max_aliases=3).applied == []     # a term with a language
    assert glossary.expand_with("ask my boss", terms, max_terms=5, max_aliases=3).search_text == "ask my boss line manager"
    assert glossary.expand_with("ask my line manager", terms, max_terms=5, max_aliases=3).applied == []   # preferred: one way only
    many = glossary.clean_terms([{"canonical": f"term{i}", "aliases": [f"a{i}x", f"b{i}x", f"c{i}x"]} for i in range(4)])
    e = glossary.expand_with("term0 term1 term2 term3", many, max_terms=2, max_aliases=1)
    assert len(e.applied) == 2 and all(len(a["added"]) == 1 for a in e.applied)


def test_conflicts_are_reported_and_block_publishing(world):
    terms = glossary.clean_terms([{"canonical": "paid time off", "aliases": ["PTO", "leave"]},
                                  {"canonical": "personal time", "aliases": ["PTO"]},
                                  {"canonical": "leave", "aliases": ["absence"]}])
    kinds = {c["kind"] for c in glossary.conflicts(terms)}
    assert kinds == {"alias_on_two_terms", "alias_is_a_term"}
    r = world.admin.put(f"{P}/glossaries/kb/{world.kb['id']}/draft", headers=world.ah, json={"terms": [t.as_dict() for t in terms]})
    assert r.status_code == 200 and len(r.json()["draft"]["conflicts"]) == 2
    r = world.admin.post(f"{P}/glossaries/versions/{r.json()['draft']['id']}/publish", headers=world.ah)
    assert r.status_code == 409


def test_invalid_terms_are_refused():
    for bad in ([{"canonical": "", "aliases": ["x"]}], [{"canonical": "x", "aliases": []}], [{"canonical": "x", "aliases": ["y"], "kind": "odd"}],
                [{"canonical": "x", "aliases": ["y"], "language": "english"}]):
        with pytest.raises(ValueError):
            glossary.clean_terms(bad)


def test_csv_round_trip(world):
    publish("kb", world.kb["id"], [{"canonical": "paid time off", "aliases": ["PTO", "earned leave"], "kind": "acronym", "stable": True}])
    r = world.admin.get(f"{P}/glossaries/kb/{world.kb['id']}/export", headers=world.ah)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and "PTO|earned leave" in r.text
    r = world.admin.post(f"{P}/glossaries/kb/{world.kb['id']}/import", headers=world.ah, json={"csv": r.text.replace("earned leave", "annual leave")})
    assert r.status_code == 200 and r.json()["draft"]["terms"][0]["aliases"] == ["PTO", "annual leave"] and r.json()["draft"]["terms"][0]["stable"]
    assert r.json()["published"]["terms"][0]["aliases"] == ["PTO", "earned leave"]           # untouched until published
    assert world.admin.post(f"{P}/glossaries/kb/{world.kb['id']}/import", headers=world.ah, json={"csv": "a,b\n1,2"}).status_code == 422


# ---- query time --------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("strategy", ["similarity", "hybrid"])
def test_a_pto_question_finds_paid_time_off_and_the_prompt_keeps_the_question(world, monkeypatch, strategy):
    publish("kb", world.kb["id"], PTO)
    turn_on(world)
    scoped_settings.put("kb", world.kb["id"], {"retrieval_strategy": strategy, "score_threshold": 0.0}, "t", world.db)
    seen = searched_texts(monkeypatch)
    r, answerer = ask(world, monkeypatch, "How many PTO days do I get?", f"web-gl-{strategy}")
    assert seen == ["How many PTO days do I get? paid time off"]
    assert r["sources"][0]["source"] == "leave.txt"
    msgs = answerer.calls[0][1]
    assert "paid time off each year" in msgs[0].content                                       # the source wording reaches the model
    assert msgs[-1].content == "How many PTO days do I get?"                                 # the question as written
    assert not any("PTO days do I get? paid time off" in m.content for m in msgs)            # the expanded string never does
    with get_db().session() as s:
        t = s.get(AnswerTrace, r["answer_id"])
        g = t.glossary[str(world.kb["id"])]
        assert g["applied"][0]["added"] == ["paid time off"] and g["versions"]["kb"]["version"] == 1
        assert t.search_text == "How many PTO days do I get? paid time off"


def test_off_by_default_and_rollback_restores_the_previous_behaviour(world, monkeypatch):
    publish("kb", world.kb["id"], PTO)
    seen = searched_texts(monkeypatch)
    ask(world, monkeypatch, "How many PTO days?", "web-gl-off")
    assert seen == ["How many PTO days?"]                                                    # mode is off until switched on
    turn_on(world)
    v2 = publish("kb", world.kb["id"], [{"canonical": "paid time off", "aliases": ["PTO", "vacation"], "kind": "acronym"}])
    r1, _ = ask(world, monkeypatch, "How many PTO days?", "web-gl-v2")
    v1 = next(v for v in glossary.view("kb", world.kb["id"])["versions"] if v["version"] == 1)
    assert world.edhr.post(f"{P}/glossaries/versions/{v1['id']}/rollback", headers=world.eh).status_code == 200
    r2, _ = ask(world, monkeypatch, "How many PTO days?", "web-gl-v1")
    assert seen[1:] == ["How many PTO days? paid time off vacation", "How many PTO days? paid time off"]
    with get_db().session() as s:
        assert s.get(AnswerTrace, r1["answer_id"]).config_snapshot_id != s.get(AnswerTrace, r2["answer_id"]).config_snapshot_id
    assert v2["version"] == 2


def test_kb_terms_beat_department_terms_and_departments_stay_apart(world, monkeypatch):
    publish("department", world.hr, [{"canonical": "personal time", "aliases": ["PTO"]}, {"canonical": "line manager", "aliases": ["boss"]}])
    publish("department", world.it, [{"canonical": "virtual private network", "aliases": ["VPN"]}])
    turn_on(world)
    e = glossary.expand("PTO and my boss and the VPN", world.kb)
    assert e.search_text == "PTO and my boss and the VPN personal time line manager"          # IT's terms never apply here
    publish("kb", world.kb["id"], PTO)
    e = glossary.expand("PTO and my boss", world.kb)
    assert e.search_text == "PTO and my boss paid time off line manager"
    assert {a["source"] for a in e.applied} == {"kb", "department"}


def test_the_allow_list_is_unchanged_for_expanded_searches(world, monkeypatch):
    secret = world.upload(world.kb, "secret.txt", "Board members get 40 days of paid time off.", visibility="people",
                          user_ids=str(world.outsider["id"]))
    assert secret["visibility"] == "people"
    publish("kb", world.kb["id"], PTO)
    turn_on(world)
    scoped_settings.put("kb", world.kb["id"], {"score_threshold": 0.0, "top_k": 10}, "t", world.db)
    r, _ = ask(world, monkeypatch, "How many PTO days?", "web-gl-acl")
    assert "secret.txt" not in [s["source"] for s in r["sources"]]
    r, _ = ask(world, monkeypatch, "How many PTO days?", "web-gl-acl2", email="gl-other@sysfore.com")
    documents.delete(world.db, secret["id"])


def test_the_test_view_expands_without_searching(world, monkeypatch):
    publish("kb", world.kb["id"], PTO)
    world.admin.put(f"{P}/glossaries/kb/{world.kb['id']}/draft", headers=world.ah,
                    json={"terms": PTO + [{"canonical": "work from home", "aliases": ["WFH"]}]})
    monkeypatch.setattr(retriever, "_search_kb", lambda *a, **k: pytest.fail("must not search"))
    body = {"kb_id": world.kb["id"], "question": "PTO while on WFH?"}
    r = world.admin.post(P + "/glossaries/test", headers=world.ah, json=body).json()
    assert r["search_text"] == "PTO while on WFH? paid time off" and r["mode"] == "off"
    r = world.admin.post(P + "/glossaries/test", headers=world.ah, json={**body, "draft_scope_type": "kb", "draft_scope_id": world.kb["id"]}).json()
    assert r["search_text"] == "PTO while on WFH? paid time off work from home"


# ---- index time ----------------------------------------------------------------------------------------------------------------
def test_index_time_embeds_stable_terms_shows_the_original_and_flags_a_reindex(world, monkeypatch):
    publish("kb", world.kb["id"], [{**PTO[0], "stable": True}])
    turn_on(world, "index")
    embedded = []
    from app.knowledge import embeddings
    real = embeddings.ChromaDefaultEmbeddings.embed_documents
    monkeypatch.setattr(embeddings.ChromaDefaultEmbeddings, "embed_documents", lambda self, texts: embedded.extend(texts) or real(self, texts))
    d = world.upload(world.kb, "pto2.txt", "Unused paid time off can be carried over once.")
    assert embedded == ["Unused paid time off can be carried over once. PTO"]
    stored = kbs.store_for(world.kb).get([f"{d['doc_key']}::0"])[0]["text"]
    assert stored == "Unused paid time off can be carried over once."                        # displayed as written
    assert documents.get(world.db, d["id"])["glossary_indexed"].endswith("kb@v1")
    view = world.admin.get(f"{P}/glossaries/kb/{world.kb['id']}", headers=world.ah).json()
    assert view["reindex"] >= 1                                                               # documents indexed before the glossary
    publish("kb", world.kb["id"], [{**PTO[0], "aliases": ["PTO", "vacation"], "stable": True}])
    assert world.admin.get(f"{P}/glossaries/kb/{world.kb['id']}", headers=world.ah).json()["reindex"] >= view["reindex"] + 1
    documents.reindex_kb(world.db, world.kb["id"])
    end = time.time() + 60
    while time.time() < end and world.admin.get(f"{P}/glossaries/kb/{world.kb['id']}", headers=world.ah).json()["reindex"]:
        time.sleep(0.3)
    assert world.admin.get(f"{P}/glossaries/kb/{world.kb['id']}", headers=world.ah).json()["reindex"] == 0
    documents.delete(world.db, d["id"])


def test_index_mode_expands_keywords_with_every_term_and_meaning_with_the_others(world, monkeypatch):
    publish("kb", world.kb["id"], [{**PTO[0], "stable": True}, {"canonical": "line manager", "aliases": ["boss"]}])
    turn_on(world, "index")
    seen = searched_texts(monkeypatch)
    ask(world, monkeypatch, "Does my boss approve PTO?", "web-gl-idx")
    assert seen == ["Does my boss approve PTO? line manager"]                                 # PTO is already in the passages


# ---- the screen and its rules ------------------------------------------------------------------------------------------------
def test_editors_manage_only_their_own_departments_glossaries_and_changes_are_audited(world):
    r = world.edhr.put(f"{P}/glossaries/kb/{world.kb['id']}/draft", headers=world.eh, json={"terms": PTO})
    assert r.status_code == 200
    assert world.edhr.post(f"{P}/glossaries/versions/{r.json()['draft']['id']}/publish", headers=world.eh).status_code == 200
    assert world.edhr.get(f"{P}/glossaries/kb/{world.kb_it['id']}", headers=world.eh).status_code == 403
    assert world.edhr.put(f"{P}/glossaries/department/{world.it}/draft", headers=world.eh, json={"terms": PTO}).status_code == 403
    assert world.edhr.post(P + "/glossaries/test", headers=world.eh, json={"kb_id": world.kb_it["id"], "question": "x"}).status_code == 404
    scopes = world.edhr.get(P + "/glossaries/scopes", headers=world.eh).json()
    assert [d["id"] for d in scopes["departments"]] == [world.hr] and world.kb_it["id"] not in [k["id"] for k in scopes["kbs"]]
    it_version = publish("department", world.it, PTO)
    assert world.edhr.post(f"{P}/glossaries/versions/{it_version['id']}/rollback", headers=world.eh).status_code == 403
    with get_db().session() as s:
        acts = [a.detail for a in s.scalars(select(AuditLog).where(AuditLog.action.like("glossary.%"), AuditLog.actor == "gl-ed-hr@sysfore.com"))]
    assert any("1 terms" in a for a in acts) and any("v1 published" in a for a in acts) and not any("paid time off" in a for a in acts)


def test_anonymous_and_missing_csrf_are_refused(world):
    anon = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))
    assert anon.get(f"{P}/glossaries/kb/{world.kb['id']}").status_code == 401
    assert world.admin.put(f"{P}/glossaries/kb/{world.kb['id']}/draft", json={"terms": PTO}).status_code == 403


def test_deleting_a_kb_removes_its_glossary(world):
    kb = world.admin.post(P + "/kbs", headers=world.ah, json={"name": "Gl temp", "department_id": world.hr}).json()
    publish("kb", kb["id"], PTO)
    kbs.delete_kb(world.db, kb["id"]) if hasattr(kbs, "delete_kb") else world.admin.delete(f"{P}/kbs/{kb['id']}", headers=world.ah)
    assert glossary.view("kb", kb["id"]) == {"versions": [], "draft": None, "published": None}
