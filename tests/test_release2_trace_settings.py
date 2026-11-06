"""Release 2, Phase 1: scoped settings and the answer trace, against the real application with real documents."""
import logging
import time
import types

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import chat_service, identity, llm, memory, prompts, rag, retention, retriever, scoped_settings, trace
from app.admin import users
from app.api import app
from app.db import AnswerTrace, AuditLog, Conversation, Department, DepartmentEditor, Message, UsageRecord, get_db
from app.knowledge import access, documents, kbs
from tests.test_baseline import FakeMessage, FakeModel

P = "/admin/api"
PW = "a good password 1"
SECRET = "ZEBRA-PASSAGE-7731"


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
        s.add(Department(name="TrHR", retention_days=30, banned_topics="", is_default=False))
        s.add(Department(name="TrIT", retention_days=30, banned_topics="", is_default=False))
    with db.session() as s:
        w.hr = s.scalar(select(Department.id).where(Department.name == "TrHR"))
        w.it = s.scalar(select(Department.id).where(Department.name == "TrIT"))
    users.create_admin(db, "tr-boss@sysfore.com", PW, domain="sysfore.com")
    w.ed_hr = person(db, "tr-ed-hr@sysfore.com", "editor", w.hr)
    w.ed_it = person(db, "tr-ed-it@sysfore.com", "editor", w.it)
    w.asker = person(db, "tr-asker@sysfore.com", "viewer", w.hr)
    with db.session() as s:
        s.add(DepartmentEditor(department_id=w.hr, user_id=w.ed_hr["id"]))
        s.add(DepartmentEditor(department_id=w.it, user_id=w.ed_it["id"]))
    w.admin, w.ah = sign_in("tr-boss@sysfore.com")
    w.edhr, w.eh = sign_in("tr-ed-hr@sysfore.com")
    w.edit, w.ei = sign_in("tr-ed-it@sysfore.com")
    w.kb_hr = w.admin.post(P + "/kbs", headers=w.ah, json={"name": "TrHR docs", "department_id": w.hr}).json()
    w.kb_it = w.admin.post(P + "/kbs", headers=w.ah, json={"name": "TrIT docs", "department_id": w.it}).json()

    def upload(c, h, kb, name, text):
        r = c.post(f"{P}/kbs/{kb['id']}/documents", headers=h, files={"file": (name, text.encode())})
        assert r.status_code == 201, r.text
        end = time.time() + 90
        while time.time() < end and documents.get(db, r.json()["id"])["status"] not in ("Indexed", "Failed"):
            time.sleep(0.2)
        return r.json()
    w.doc_hr = upload(w.edhr, w.eh, w.kb_hr, "tr-hr.txt", f"The annual leave policy gives every employee 18 days. Code {SECRET}.")
    w.doc_it = upload(w.edit, w.ei, w.kb_it, "tr-it.txt", "The annual leave calendar for the IT team is kept in the portal. Code ITCODE-991.")
    yield w
    documents.shutdown()


@pytest.fixture(autouse=True)
def clean_overrides():
    yield
    from app.db import ScopedSetting
    with get_db().session() as s:
        s.execute(delete(ScopedSetting))
    scoped_settings.invalidate()


def ask(monkeypatch, w, question="annual leave?", conv="web-tr-1", kb_ids=None, email="tr-asker@sysfore.com", answer="18 days [1]."):
    monkeypatch.setattr(chat_service, "get_chat_model", lambda: FakeModel(reply='{"search": true, "query": "annual leave policy"}'))
    monkeypatch.setattr(rag, "get_chat_model", lambda: FakeModel(parts=[answer]))
    token = identity.bind(identity.resolve(email, w.db))
    try:
        return chat_service.chat(question, conv, kb_ids=kb_ids or [w.kb_hr["id"]])
    finally:
        identity.unbind(token)


def trace_row(db, answer_id):
    with db.session() as s:
        t = s.get(AnswerTrace, answer_id)
        s.expunge(t) if t else None
        return t


# ---- scoped settings: precedence and registry -----------------------------------------------------------------------
def test_kb_beats_department_beats_global(world):
    db, kb = world.db, world.kb_hr
    base = scoped_settings.for_kb(kb, "top_k", db)
    scoped_settings.put("department", world.hr, {"top_k": base + 2}, "t", db)
    assert scoped_settings.explain("top_k", kb_id=kb["id"], department_id=world.hr, db=db) == (base + 2, "department")
    scoped_settings.put("kb", kb["id"], {"top_k": base + 4}, "t", db)
    assert scoped_settings.explain("top_k", kb_id=kb["id"], department_id=world.hr, db=db) == (base + 4, "kb")
    assert scoped_settings.reset("kb", kb["id"], "top_k", db) is True
    assert scoped_settings.for_kb(kb, "top_k", db) == base + 2
    assert scoped_settings.get("top_k", kb_id=world.kb_it["id"], department_id=world.it, db=db) == base     # others keep the global value


def test_channel_beats_kb_for_a_key_that_allows_it(world, monkeypatch):
    monkeypatch.setitem(scoped_settings.REGISTRY, "top_k", scoped_settings.Key("top_k", ("channel", "kb", "department")))
    scoped_settings.put("kb", 1, {"top_k": 7}, "t", world.db)
    scoped_settings.put("channel", "slack", {"top_k": 9}, "t", world.db)
    assert scoped_settings.explain("top_k", channel="slack", kb_id=1, db=world.db) == (9, "channel")
    assert scoped_settings.explain("top_k", channel="teams", kb_id=1, db=world.db) == (7, "kb")


def test_a_setting_cannot_be_set_at_a_scope_the_registry_does_not_allow(world):
    with pytest.raises(ValueError, match="cannot be set for a department"):
        scoped_settings.put("department", world.hr, {"score_threshold": 0.5}, "t", world.db)
    with pytest.raises(ValueError, match="cannot be set for a channel"):
        scoped_settings.put("channel", "slack", {"top_k": 5}, "t", world.db)
    with pytest.raises(ValueError, match="Unknown setting"):
        scoped_settings.put("kb", 1, {"chunk_size": 500}, "t", world.db)
    r = world.admin.put(f"{P}/scoped-settings/department/{world.hr}", headers=world.ah, json={"values": {"score_threshold": 0.5}})
    assert r.status_code == 422


def test_values_are_validated_and_a_bad_batch_changes_nothing(world):
    with pytest.raises(ValueError):
        scoped_settings.put("kb", 5, {"top_k": 8, "score_threshold": 7}, "t", world.db)
    with pytest.raises(ValueError):
        scoped_settings.put("kb", 5, {"retrieval_strategy": "psychic"}, "t", world.db)
    assert scoped_settings.view("kb", 5, db=world.db)[0]["overridden"] is False


def test_an_override_reaches_the_next_question_with_no_restart(world):
    db, kb = world.db, world.kb_hr
    assert retriever.threshold_for(kb) == 0.35
    scoped_settings.put("kb", kb["id"], {"score_threshold": 0.9}, "t", db)       # the write clears the cache
    assert retriever.threshold_for(kb) == 0.9
    assert retriever.threshold_for(world.kb_it) == 0.35


def test_retrieval_uses_the_settings_of_each_knowledge_base(world):
    db = world.db
    token = identity.bind(identity.resolve("tr-boss@sysfore.com", db))
    try:
        assert retriever.retrieve("annual leave", kb_ids=[world.kb_hr["id"]])
        scoped_settings.put("kb", world.kb_hr["id"], {"score_threshold": 1.0}, "t", db)
        assert retriever.retrieve("annual leave", kb_ids=[world.kb_hr["id"]]) == []
        both = retriever.retrieve("annual leave", kb_ids=[world.kb_hr["id"], world.kb_it["id"]])
        assert {c["kb_id"] for c in both} == {world.kb_it["id"]}                 # one KB's threshold does not touch the other
        scoped_settings.reset("kb", world.kb_hr["id"], "score_threshold", db)
        scoped_settings.put("kb", world.kb_hr["id"], {"retrieval_strategy": "keyword", "score_threshold": 0.0}, "t", db)
        assert retriever.retrieve("annual leave", kb_ids=[world.kb_hr["id"]])[0]["via"] == "keywords"
    finally:
        identity.unbind(token)


def test_settings_of_a_deleted_knowledge_base_are_removed(world):
    kb = world.admin.post(P + "/kbs", headers=world.ah, json={"name": "TrGone", "department_id": world.hr}).json()
    scoped_settings.put("kb", kb["id"], {"top_k": 9}, "t", world.db)
    assert world.admin.delete(f"{P}/kbs/{kb['id']}", headers=world.ah).status_code == 200
    assert scoped_settings.get("top_k", kb_id=kb["id"], db=world.db) != 9


def test_an_unreadable_table_falls_back_to_the_global_value(world, monkeypatch):
    scoped_settings.invalidate()
    broken = types.SimpleNamespace(session=lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert scoped_settings._overrides(broken) == {}


# ---- scoped settings: API and roles ----------------------------------------------------------------------------------
def test_screen_shows_inherited_then_overridden_then_reset_and_audits(world):
    url = f"{P}/scoped-settings/kb/{world.kb_hr['id']}"
    field = lambda body: next(f for f in body["fields"] if f["key"] == "top_k")
    world.edhr.put(f"{P}/scoped-settings/department/{world.hr}", headers=world.eh, json={"values": {"top_k": 8}})
    got = field(world.edhr.get(url).json())
    assert (got["overridden"], got["value"], got["inherited_from"]) == (False, 8, "department")
    got = field(world.edhr.put(url, headers=world.eh, json={"values": {"top_k": 11}}).json())
    assert (got["overridden"], got["value"], got["inherited"], got["updated_by"]) == (True, 11, 8, "tr-ed-hr@sysfore.com")
    got = field(world.edhr.delete(url + "/top_k", headers=world.eh).json())
    assert (got["overridden"], got["value"]) == (False, 8)
    with world.db.session() as s:
        details = [a.detail for a in s.scalars(select(AuditLog).where(AuditLog.target == f"kb:{world.kb_hr['id']}"))]
    assert "top_k: inherited -> 11" in details and "top_k: back to inherited" in details
    assert "score_threshold" not in [f["key"] for f in world.edhr.get(f"{P}/scoped-settings/department/{world.hr}").json()["fields"]]


def test_editors_are_limited_to_their_own_department_and_its_kbs(world):
    other_kb, other_dept = f"{P}/scoped-settings/kb/{world.kb_it['id']}", f"{P}/scoped-settings/department/{world.it}"
    body = {"values": {"top_k": 9}}
    for r in (world.edhr.get(other_kb), world.edhr.put(other_kb, headers=world.eh, json=body), world.edhr.delete(other_kb + "/top_k", headers=world.eh),
              world.edhr.get(other_dept), world.edhr.put(other_dept, headers=world.eh, json=body)):
        assert r.status_code == 403, r.text
    assert world.edhr.get(f"{P}/scoped-settings/channel/slack").status_code == 403
    scopes = world.edhr.get(P + "/scoped-settings/scopes").json()
    assert [k["id"] for k in scopes["kbs"]] == [world.kb_hr["id"]] and [d["id"] for d in scopes["departments"]] == [world.hr] and scopes["channels"] == []


def test_a_viewer_has_no_panel_and_anonymous_is_refused(world):
    anon = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))
    for path in ("/scoped-settings/scopes", "/traces/1", "/traces/conversations"):
        assert anon.get(P + path).status_code == 401
    assert anon.post(P + "/auth/login", json={"email": "tr-asker@sysfore.com", "password": PW}).status_code in (401, 403)


def test_a_change_needs_the_csrf_header(world):
    assert world.edhr.put(f"{P}/scoped-settings/kb/{world.kb_hr['id']}", json={"values": {"top_k": 9}}).status_code == 403


# ---- the trace --------------------------------------------------------------------------------------------------------
def test_every_answer_has_one_trace_with_references_and_no_text(world, monkeypatch):
    out = ask(monkeypatch, world, conv="web-tr-a")
    t = trace_row(world.db, out["answer_id"])
    assert (t.complete, t.found, t.searched) == (True, True, True)
    assert t.conversation_id == "web-tr-a" and t.standalone_query == t.search_text == "annual leave policy"
    hit = t.hits[0]
    assert hit["kb_id"] == world.kb_hr["id"] and hit["doc_id"] == world.doc_hr["id"] and hit["chunk_id"] and hit["rank"] == 1 and hit["used"] is True
    assert hit["via"] == "meaning" and "score" in hit
    r = t.retrieval[0]
    assert r["kb_id"] == world.kb_hr["id"] and r["threshold"] == 0.35 and r["top_k"] == 5 and r["retrieval_strategy"] == "similarity" and r["index_version"] == 1
    assert set(t.timings) >= {"input_checks", "routing", "retrieval", "generation"} and t.config_snapshot_id
    assert "base-answer" in t.prompt_versions
    assert SECRET not in str({c.name: getattr(t, c.name) for c in AnswerTrace.__table__.columns})     # references, not passage text
    with world.db.session() as s:
        assert len(list(s.scalars(select(AnswerTrace).where(AnswerTrace.conversation_id == "web-tr-a")))) == 1


def test_small_talk_is_traced_without_hits_and_a_preview_is_not_traced(world, monkeypatch):
    monkeypatch.setattr(chat_service, "get_chat_model", lambda: FakeModel(reply='{"search": false}'))
    monkeypatch.setattr(rag, "get_chat_model", lambda: FakeModel(parts=["Hello!"]))
    out = chat_service.chat("hi", "web-tr-small")
    t = trace_row(world.db, out["answer_id"])
    assert t.searched is False and t.hits == [] and t.found is True
    with world.db.session() as s:
        before = len(list(s.scalars(select(AnswerTrace.answer_id))))
    tokens, meta = chat_service.chat_stream("hi", None, preview=True)
    assert "".join(tokens) == "Hello!" and meta["answer_id"] is None
    with world.db.session() as s:
        assert len(list(s.scalars(select(AnswerTrace.answer_id)))) == before


def test_not_found_is_traced_with_passages_below_the_threshold(world, monkeypatch):
    scoped_settings.put("kb", world.kb_hr["id"], {"score_threshold": 1.0}, "t", world.db)
    out = ask(monkeypatch, world, conv="web-tr-nf")
    t = trace_row(world.db, out["answer_id"])
    assert out["found"] is False and t.found is False and t.retrieval[0]["threshold"] == 1.0
    assert t.hits and not any(h["above_threshold"] or h["used"] for h in t.hits)


def test_the_snapshot_changes_with_the_settings(world, monkeypatch):
    first = trace_row(world.db, ask(monkeypatch, world, conv="web-tr-s1")["answer_id"]).config_snapshot_id
    same = trace_row(world.db, ask(monkeypatch, world, conv="web-tr-s2")["answer_id"]).config_snapshot_id
    scoped_settings.put("kb", world.kb_hr["id"], {"top_k": 7}, "t", world.db)
    changed = trace_row(world.db, ask(monkeypatch, world, conv="web-tr-s3")["answer_id"]).config_snapshot_id
    assert first == same != changed


def test_a_failing_trace_write_still_delivers_the_answer_and_logs_no_text(world, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("the secret question text")
    monkeypatch.setattr(trace, "AnswerTrace", boom)
    with caplog.at_level(logging.WARNING, logger="app.trace"):
        out = ask(monkeypatch, world, conv="web-tr-fail")
    assert out["answer"] == "18 days [1]." and memory.get_messages("web-tr-fail", world.db)[1]["content"] == out["answer"]
    assert "save failed (RuntimeError)" in caplog.text and "secret question" not in caplog.text


def test_a_failed_model_call_leaves_one_incomplete_trace(world, monkeypatch):
    monkeypatch.setattr(chat_service, "get_chat_model", lambda: FakeModel(reply='{"search": true, "query": "annual leave policy"}'))

    def broken(msgs, **kw):
        yield FakeMessage("part")
        raise RuntimeError("provider died")
    monkeypatch.setattr(rag, "get_chat_model", lambda: types.SimpleNamespace(stream=broken))
    token = identity.bind(identity.resolve("tr-asker@sysfore.com", world.db))
    try:
        tokens, meta = chat_service.chat_stream("annual leave", "web-tr-broken", kb_ids=[world.kb_hr["id"]])
        with pytest.raises(RuntimeError):
            "".join(tokens)
    finally:
        identity.unbind(token)
    assert trace_row(world.db, meta["answer_id"]).complete is False


def test_the_trace_names_the_model_actually_used_including_a_fallback(world, monkeypatch):
    class Down:
        def stream(self, msgs, **kw):
            raise TimeoutError("slow")

    class Up:
        def stream(self, msgs, **kw):
            return iter([FakeMessage("18 days [1].")])
    chain = llm.ModelChain([llm.Slot(1, "First", "openai", "m1", Down()), llm.Slot(2, "Second", "openai", "m2", Up())], world.db)
    monkeypatch.setattr(chat_service, "get_chat_model", lambda: FakeModel(reply='{"search": true, "query": "annual leave"}'))
    monkeypatch.setattr(rag, "get_chat_model", lambda: chain)
    token = identity.bind(identity.resolve("tr-asker@sysfore.com", world.db))
    try:
        out = chat_service.chat("annual leave?", "web-tr-fb", kb_ids=[world.kb_hr["id"]])
    finally:
        identity.unbind(token)
    t = trace_row(world.db, out["answer_id"])
    assert (t.provider, t.model, t.fallback_used) == ("Second", "m2", True)
    view = world.admin.get(f"{P}/traces/{out['answer_id']}").json()
    assert [(u["model"], u["status"]) for u in view["usage"]] == [("m1", "failed"), ("m2", "ok")]       # the linked usage rows


def test_delete_and_retention_move_a_conversation_aside_with_its_trace(world, monkeypatch):
    """D62: nothing is purged. The conversation, its messages and its trace get a new id, so no screen or list shows them, and the
    trace is still there for the review queue and the replay."""
    from datetime import datetime, timedelta, timezone
    a = ask(monkeypatch, world, conv="web-tr-del")["answer_id"]
    assert memory.delete("web-tr-del", world.db)
    row = trace_row(world.db, a)
    assert row is not None and row.conversation_id != "web-tr-del" and "web-tr-del" in row.conversation_id
    assert memory.get_messages("web-tr-del", world.db) == [] and "web-tr-del" not in [c["id"] for c in memory.list_conversations(db=world.db)]
    b = ask(monkeypatch, world, conv="web-tr-old")["answer_id"]
    with world.db.session() as s:
        s.get(Conversation, "web-tr-old").updated_at = datetime.now(timezone.utc) - timedelta(days=4000)
    retention.run(world.db)
    assert trace_row(world.db, b) is not None and trace_row(world.db, b).conversation_id != "web-tr-old"
    with world.db.session() as s:
        assert s.get(Conversation, "web-tr-old") is None
        moved = s.scalars(select(Conversation).where(Conversation.deleted_at.is_not(None), Conversation.id.like("%web-tr-old"))).all()
        assert len(moved) == 1 and list(s.scalars(select(Message).where(Message.conversation_id == moved[0].id)))
        assert s.scalars(select(Message).where(Message.conversation_id == "web-tr-old")).first() is None


# ---- the trace API and who sees what -----------------------------------------------------------------------------------
def test_admin_sees_the_passage_and_the_view_is_audited_without_text(world, monkeypatch):
    aid = ask(monkeypatch, world, conv="web-tr-view")["answer_id"]
    r = world.admin.get(f"{P}/traces/{aid}")
    assert r.status_code == 200, r.text
    hit = r.json()["hits"][0]
    assert hit["status"] == "visible" and SECRET in hit["text"] and hit["title"] == "tr-hr.txt" and hit["kb"] == "TrHR docs"
    with world.db.session() as s:
        row = s.scalars(select(AuditLog).where(AuditLog.action == "trace.view", AuditLog.target == str(aid))).first()
    assert row.actor == "tr-boss@sysfore.com" and SECRET not in row.detail


def test_an_editor_only_opens_their_own_departments_traces(world, monkeypatch):
    aid = ask(monkeypatch, world, conv="web-tr-dept")["answer_id"]
    assert world.edhr.get(f"{P}/traces/{aid}").status_code == 200
    assert world.edit.get(f"{P}/traces/{aid}").status_code == 404
    assert world.edit.get(f"{P}/traces/conversations/web-tr-dept").status_code == 404
    ids = [c["id"] for c in world.edit.get(f"{P}/traces/conversations").json()["conversations"]]
    assert "web-tr-dept" not in ids
    assert "web-tr-dept" in [c["id"] for c in world.edhr.get(f"{P}/traces/conversations?limit=200").json()["conversations"]]
    assert world.edhr.get(f"{P}/conversations").status_code == 403                      # the Admin routes stay Admin only
    assert world.edhr.delete(f"{P}/conversations/web-tr-dept", headers=world.eh).status_code == 403


def test_an_editors_conversation_view_has_no_stored_snippets(world, monkeypatch):
    ask(monkeypatch, world, conv="web-tr-snip")
    body = world.edhr.get(f"{P}/traces/conversations/web-tr-snip").json()
    assert body["messages"][1]["content"] == "18 days [1]." and all("meta" not in m for m in body["messages"]) and SECRET not in str(body)


def test_a_document_the_viewer_cannot_see_shows_as_restricted(world, monkeypatch):
    aid = ask(monkeypatch, world, conv="web-tr-multi", kb_ids=[world.kb_hr["id"], world.kb_it["id"]], email="tr-boss@sysfore.com")["answer_id"]
    with world.db.session() as s:
        s.get(Conversation, "web-tr-multi").department_id = world.hr                    # an HR conversation that drew on an IT passage
    access.set_kb_query_groups(world.db, world.kb_it["id"], [])
    access.set_access(world.db, world.doc_it["id"], "people", user_ids=[world.ed_it["id"]])
    try:
        hits = world.edhr.get(f"{P}/traces/{aid}").json()["hits"]
        mine = next(h for h in hits if h["kb_id"] == world.kb_hr["id"])
        theirs = next(h for h in hits if h["kb_id"] == world.kb_it["id"])
        assert mine["status"] == "visible"
        assert theirs["status"] == "restricted" and theirs["text"] == "" and theirs["title"] == "" and theirs["chunk_id"] is None and theirs["doc_id"] is None
        assert "ITCODE" not in str(hits) and "tr-it.txt" not in str(hits)
        assert "ITCODE-991" in str(world.admin.get(f"{P}/traces/{aid}").json()["hits"])  # an Admin may see any document
    finally:
        access.set_access(world.db, world.doc_it["id"], "everyone")


def test_the_chunks_own_access_has_the_last_word(world, monkeypatch):
    aid = ask(monkeypatch, world, conv="web-tr-chunk")["answer_id"]
    real = kbs.store_for

    def stale(kb, *a, **k):
        store = real(kb, *a, **k)
        got = store.get
        store.get = lambda ids: [{**r, "metadata": {**r["metadata"], "doc_id": "not-a-visible-document"}} for r in got(ids)]
        return store
    monkeypatch.setattr(kbs, "store_for", stale)
    assert world.edhr.get(f"{P}/traces/{aid}").json()["hits"][0]["status"] == "restricted"


def test_a_deleted_document_is_unavailable_and_a_store_failure_breaks_nothing(world, monkeypatch):
    aid = ask(monkeypatch, world, conv="web-tr-gone")["answer_id"]
    monkeypatch.setattr(kbs, "store_for", lambda kb, *a, **k: (_ for _ in ()).throw(RuntimeError("store down")))
    r = world.admin.get(f"{P}/traces/{aid}")
    assert r.status_code == 200 and r.json()["hits"][0]["status"] == "unavailable" and r.json()["hits"][0]["text"] == ""
    assert world.admin.get(f"{P}/traces/999999").status_code == 404


def test_usage_rows_are_linked_to_the_answer(world, monkeypatch):
    aid = ask(monkeypatch, world, conv="web-tr-usage")["answer_id"]
    with world.db.session() as s:
        s.add(UsageRecord(provider="p", model="m", purpose="answer", status="ok", message_id=aid))
    assert [u["model"] for u in world.admin.get(f"{P}/traces/{aid}").json()["usage"]] == ["m"]
