"""Release 2, Phase 0: the groundwork the later features rely on.

Answer identity, who asked, ids on sources, the person's own question reaching the answer step, masking as its own
step, the language-neutral not-found marker, the threshold applied per knowledge base before merging, Unicode-aware
keyword words, and model usage linked to its answer. Fakes stand in for the models and the vector store.
"""
import types
import uuid

import pytest
from sqlalchemy import select

from app import chat_service, guardrails, identity, keyword_search, memory, prompts, rag, retriever, usage
from app.channels import core
from app.channels.base import Envelope
from app.db import Message, UsageRecord, get_db


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeModel:
    def __init__(self, reply="", parts=None):
        self.reply, self.parts, self.calls = reply, parts or [], []

    def invoke(self, msgs, **kw):
        self.calls.append(msgs)
        return FakeMessage(self.reply)

    def stream(self, msgs, **kw):
        self.calls.append(msgs)
        for p in self.parts:
            yield FakeMessage(p)


CHUNKS = [{"text": "Earned leave is 18 days.", "source": "HR-Policy.pdf", "page": 4, "score": 0.9,
           "doc_id": 7, "kb_id": 1, "chunk_id": "doc-7::0", "title": "HR-Policy.pdf"}]


@pytest.fixture
def turn(monkeypatch):
    """One question through chat_service with a fake router and answer model. Returns ask(text, answer, route)."""
    state = {}

    def ask(text, answer_parts, route='{"search": true, "query": "earned leave days"}', chunks=CHUNKS, conv=None):
        router, answerer = FakeModel(reply=route), FakeModel(parts=answer_parts)
        monkeypatch.setattr(chat_service, "get_chat_model", lambda: router)
        monkeypatch.setattr(rag, "get_chat_model", lambda: answerer)
        def search(q, kb_ids=None):
            if state.get("on_search"):
                state["on_search"]()
            return [dict(c) for c in chunks]
        monkeypatch.setattr(rag, "search", search)
        conv = conv or f"web-test-{uuid.uuid4().hex[:8]}"
        state.update(router=router, answerer=answerer, conv=conv)
        return chat_service.chat(text, conv)
    yield ask, state
    if state.get("conv"):
        memory.delete(state["conv"])


# ---- answer identity and who asked ------------------------------------------------------------------------------------
def test_every_saved_answer_has_an_id_that_names_the_stored_message(turn):
    ask, state = turn
    who = identity.Identity(email="amy@sysfore.com", user_id=41, known=True)
    tok = identity.bind(who)
    try:
        out = ask("How many earned leaves?", ["Employees get 18 days [1]."])
    finally:
        identity.unbind(tok)
    assert isinstance(out["answer_id"], int) and out["found"] is True
    msgs = memory.get_messages(state["conv"])
    assert [m["role"] for m in msgs] == ["user", "assistant"] and msgs[1]["id"] == out["answer_id"]
    with get_db().session() as s:
        q = s.get(Message, msgs[0]["id"])
        assert (q.author_user_id, q.author_email) == (41, "amy@sysfore.com")          # several people can ask in a thread
        assert s.get(Message, out["answer_id"]).author_email == ""                   # answers have no author


def test_a_preview_has_no_answer_id(monkeypatch):
    monkeypatch.setattr(chat_service, "get_chat_model", lambda: FakeModel(reply='{"search": false}'))
    monkeypatch.setattr(rag, "get_chat_model", lambda: FakeModel(parts=["hello"]))
    tokens, meta = chat_service.chat_stream("hi", None, preview=True)
    assert "".join(tokens) == "hello" and meta["answer_id"] is None


def test_channel_replies_that_are_not_answers_carry_no_answer_id(monkeypatch):
    monkeypatch.setattr(chat_service, "chat", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model down")))
    out = core.handle(Envelope("slack", "T", "C", "1.0", "U", "hello"))
    assert out["answer"] == core.ERROR_REPLY and out["answer_id"] is None and out["found"] is False


# ---- the person's own question reaches the answer step, in their language -----------------------------------------
def test_the_answer_step_gets_the_original_question_plus_its_standalone_meaning(turn):
    ask, state = turn
    ask("what about contractors?", ["Contractors get 12 days [1]."],
        route='{"search": true, "query": "earned leave entitlement for contractors"}')
    human = state["answerer"].calls[0][1].content
    assert human.startswith("what about contractors?")
    assert "earned leave entitlement for contractors" in human


def test_a_self_contained_question_is_sent_as_written(turn):
    ask, state = turn
    ask("Earned leave days", ["18 days [1]."], route='{"search": true, "query": "earned leave  days"}')
    assert state["answerer"].calls[0][1].content == "Earned leave days"


def test_the_router_is_told_to_keep_the_users_language(turn):
    ask, state = turn
    ask("छुट्टी कितनी है?", ["18 दिन [1]."], route='{"search": true, "query": "अर्जित छुट्टी कितने दिन"}')
    assert "Never translate" in state["router"].calls[0][0].content
    assert state["answerer"].calls[0][1].content.startswith("छुट्टी कितनी है?")


# ---- not found, whatever the wording ------------------------------------------------------------------------------------
@pytest.mark.parametrize("reply", [prompts.NOT_FOUND_MARKER, f" {prompts.NOT_FOUND_MARKER}\n",
                                   "I couldn't find this in the knowledge base."])
def test_not_found_is_recognised_and_the_marker_never_reaches_a_person(turn, reply):
    ask, state = turn
    out = ask("What is the canteen menu?", [reply[:5], reply[5:]])            # the marker can arrive in pieces
    assert out["answer"] == rag.FALLBACK and out["found"] is False and out["sources"] == []
    stored = memory.get_messages(state["conv"])[1]
    assert stored["content"] == rag.FALLBACK and stored["meta"]["found"] is False


def test_nothing_retrieved_is_not_found_without_a_model_call(turn):
    ask, state = turn
    out = ask("What is the canteen menu?", ["should not be used"], chunks=[])
    assert out["found"] is False and out["answer"] == rag.FALLBACK and not state["answerer"].calls


def test_the_reply_rules_are_in_the_answer_prompt_only():
    assert prompts.REPLY_RULES in prompts.assemble("answer", "[1] ctx")
    assert prompts.REPLY_RULES not in prompts.assemble("chat")


# ---- sources carry ids --------------------------------------------------------------------------------------------------
def test_sources_carry_document_kb_and_passage_ids(turn):
    ask, _ = turn
    out = ask("How many earned leaves?", ["18 days [1]."])
    s = out["sources"][0]
    assert (s["doc_id"], s["kb_id"], s["chunk_id"], s["title"]) == (7, 1, "doc-7::0", "HR-Policy.pdf")


def test_retrieval_returns_the_document_id_of_each_passage(monkeypatch, open_document):
    from app.db import Document

    class Col:
        def count(self):
            return 1

        def query(self, query_texts, n_results, where=None):
            return {"ids": [["doc-open-test::0"]], "documents": [["text"]], "distances": [[0.2]],
                    "metadatas": [[{"source": "open-test.pdf", "doc_id": "doc-open-test", "page": 2}]]}
    monkeypatch.setattr(retriever, "get_collection", lambda: Col())
    hit = retriever.retrieve("anything")[0]
    with get_db().session() as s:
        expected = s.scalar(select(Document.id).where(Document.doc_key == "doc-open-test"))
    assert hit["doc_id"] == expected and hit["title"] == "open-test.pdf" and hit["kb_id"] is not None


# ---- threshold per knowledge base, before merging ----------------------------------------------------------------------
def test_each_knowledge_base_uses_its_own_threshold_before_the_merge(monkeypatch):
    seen = {}
    rows = {1: [0.92, 0.81, 0.78], 2: [0.40, 0.31, 0.20]}                 # e5-like scores vs MiniLM-like scores

    def search_kb(kb, query, k, who, min_score=None):
        seen[kb["id"]] = min_score
        hits = [{"id": f"{kb['id']}-{n}", "text": f"{kb['id']}-{n}", "score": sc, "metadata": {"source": f"kb{kb['id']}"},
                 "kb_id": kb["id"], "doc_pk": None} for n, sc in enumerate(rows[kb["id"]])]
        return [h for h in hits if min_score is None or h["score"] >= min_score]
    monkeypatch.setattr(retriever, "_targets", lambda kb_ids: [{"id": 1}, {"id": 2}])
    monkeypatch.setattr(retriever, "_search_kb", search_kb)
    monkeypatch.setattr(retriever, "threshold_for", lambda kb: {1: 0.8, 2: 0.3}[kb["id"]])
    got = retriever.retrieve("q", kb_ids=[1, 2])
    assert seen == {1: 0.8, 2: 0.3}
    assert sorted(h["text"] for h in got) == ["1-0", "1-1", "2-0", "2-1"]   # 0.78 and 0.20 fail their own KB's bar


def test_without_the_threshold_flag_nothing_is_filtered(monkeypatch):
    seen = {}
    monkeypatch.setattr(retriever, "_targets", lambda kb_ids: [{"id": 1}])
    monkeypatch.setattr(retriever, "_search_kb", lambda kb, q, k, who, min_score=None: seen.setdefault("m", min_score) and [])
    retriever.retrieve_scored("q")
    assert seen == {"m": None}


# ---- masking is its own step --------------------------------------------------------------------------------------------
@pytest.fixture
def guards(monkeypatch):
    guardrails.update({"enabled": True, "mask_enabled": True, "injection_enabled": True, "output_checks_enabled": False}, "tests")
    monkeypatch.setattr(guardrails, "mask", lambda text, types_, names=False, allow=(): (text.replace("98450 12345", "<PHONE_NUMBER>"), {"PHONE_NUMBER": 1}))
    yield
    guardrails.update({"enabled": False}, "tests")


def test_the_checks_do_not_mask_and_masking_does(guards):
    assert guardrails.check_input("call 98450 12345").text == "call 98450 12345"
    assert guardrails.mask_input("call 98450 12345").text == "call <PHONE_NUMBER>"
    assert guardrails.guard_input("call 98450 12345").text == "call <PHONE_NUMBER>"


def test_only_masked_text_is_stored_and_sent_on(guards, turn):
    ask, state = turn
    ask("my number is 98450 12345, how many leaves?", ["18 days [1]."])
    assert "98450" not in memory.get_messages(state["conv"])[0]["content"]
    assert all("98450" not in m.content for call in state["answerer"].calls + state["router"].calls for m in call)


def test_masking_that_breaks_refuses_the_question(guards, monkeypatch, turn):
    ask, state = turn
    monkeypatch.setattr(guardrails, "mask", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model")))
    out = ask("call 98450 12345", ["never"])
    assert out["answer"] == guardrails.FRIENDLY_ERROR and not state["router"].calls
    assert memory.get_messages(state["conv"]) == []                         # nothing stored unmasked


# ---- keyword search keeps whole words in every script -------------------------------------------------------------------
def test_keyword_words_keep_combining_marks():
    assert keyword_search.tokens("छुट्टी नीति क्या है") == ["छुट्टी", "नीति", "क्या", "है"]
    assert keyword_search.tokens("Café résumé") == ["café", "résumé"]
    assert keyword_search.tokens("annual leave days") == ["annual", "leave", "day"]     # English behaviour unchanged


# ---- model usage is linked to the answer it served ---------------------------------------------------------------------
def test_usage_rows_name_their_answer_and_long_purposes_fit():
    db = get_db()
    with usage.for_answer(987654):
        usage.record(db, provider="p", provider_type="openai", model="m", purpose="feedback-classify-long",
                     department=None, input_tokens=1, output_tokens=1, status="ok")
    usage.record(db, provider="p", provider_type="openai", model="m", purpose="answer", department=None,
                 input_tokens=1, output_tokens=1, status="ok")
    with db.session() as s:
        rows = list(s.scalars(select(UsageRecord).where(UsageRecord.provider == "p").order_by(UsageRecord.id.desc()).limit(2)))
        assert [r.message_id for r in rows] == [None, 987654]
        assert rows[1].purpose == "feedback-classify-long"
        for r in rows:
            s.delete(r)


def test_model_calls_during_a_turn_are_recorded_against_its_answer(turn, monkeypatch):
    ask, state = turn
    seen = []
    monkeypatch.setattr(usage, "record", lambda *a, **k: seen.append(usage._answer.get()))
    state["on_search"] = lambda: usage.record(None)                          # e.g. embedding the query
    out = ask("How many earned leaves?", ["18 days [1]."], chunks=CHUNKS)
    assert seen and set(seen) == {out["answer_id"]}
    assert usage._answer.get() is None                                       # nothing leaks past the turn


def test_the_api_returns_the_answer_id_and_found(monkeypatch):
    from fastapi.testclient import TestClient
    from app import api
    from app.security import Caller, require_chat_caller
    monkeypatch.setattr(chat_service, "chat_stream", lambda m, c, **k: (iter(["18 days"]), {
        "searched": True, "query": "q", "sources": [], "found": True, "answer_id": 55}))
    api.app.dependency_overrides[require_chat_caller] = lambda: Caller(email="amy@sysfore.com", prefix="web-1-")
    try:
        r = TestClient(api.app).post("/chat", json={"message": "hi", "conv_id": "c1"})
    finally:
        api.app.dependency_overrides.clear()
    assert r.status_code == 200 and r.json()["answer_id"] == 55 and r.json()["found"] is True
