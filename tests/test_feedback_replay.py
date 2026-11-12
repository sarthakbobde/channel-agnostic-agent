"""Release 3, Phase 3: replay verification against the real pipeline and real documents (D53). The models are stand-ins; retrieval, access,
prompts, the trace and the stores are real."""
import json
import types
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select, update

from app import agent_runtime, chat_service, feedback_flow, global_settings, identity, prompts, rag, replay, ticket_agent
from app.db import AnswerTrace, Document, Feedback, Message, Prompt, Suggestion, Ticket, TicketEvent, UsageRecord, get_db
from tests.test_baseline import FakeModel
from tests.test_feedback_capture import ask, rate, setup, world  # noqa: F401  (fixtures and helpers)

ROUTE = '{"search": true, "query": "annual leave carry over"}'


class Spy(FakeModel):
    """Records which purpose each call was for."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self.purposes = []

    def invoke(self, msgs, **kw):
        self.purposes.append(kw.get("purpose"))
        return super().invoke(msgs, **kw)

    def stream(self, msgs, **kw):
        self.purposes.append(kw.get("purpose"))
        yield from super().stream(msgs, **kw)


@pytest.fixture
def spy(monkeypatch):
    m = Spy(reply=ROUTE, parts=["Up to five days can be carried over [1]."])
    monkeypatch.setattr(chat_service, "get_chat_model", lambda: m)
    monkeypatch.setattr(rag, "get_chat_model", lambda: m)
    monkeypatch.setattr(replay, "get_chat_model", lambda: types.SimpleNamespace(
        invoke=lambda msgs, **kw: types.SimpleNamespace(content=json.dumps({"verdict": "confirmed", "reason": "The same figure is given."}))))
    return m


def rated(world, conv="web-1-rp", comment="The carry-over limit is wrong"):
    r = ask(world, conv=conv)
    rate(world, r, "down", comment=comment)
    with get_db().session() as s:
        return s.scalar(select(Feedback).where(Feedback.answer_id == r["answer_id"]))


def fresh(fb):
    with get_db().session() as s:
        row = s.get(Feedback, fb.id)
        s.expunge(row)
        return row


def counts():
    with get_db().session() as s:
        return tuple(s.scalar(select(func.count()).select_from(t)) for t in (Message, AnswerTrace, Suggestion))


@pytest.fixture(autouse=True)
def _clean_flow(world):
    yield
    with get_db().session() as s:
        s.execute(delete(TicketEvent))
        s.execute(delete(Ticket))
    with get_db().session() as s:
        s.execute(delete(UsageRecord).where(UsageRecord.purpose.like("fb_%")))
    global_settings.update({"feedback_processing_mode": "off", "feedback_replay_daily_cap": 50}, "t", get_db())


def test_a_replay_saves_no_message_no_trace_and_no_suggestion_and_records_its_calls_as_fb_replay(world, spy):
    fb = rated(world)
    before = counts()
    spy.purposes.clear()
    out = replay.replay(get_db(), fb)
    assert counts() == before
    assert out["found"] and "five days" in out["answer"].lower() and spy.purposes == ["fb_replay", "fb_replay"]      # the router and the answer
    assert replay.possible(get_db(), fb) == (True, "")


def test_the_replay_gets_the_earlier_turns_of_the_conversation(world, spy):
    ask(world, conv="web-1-rp-hist")
    fb = rated(world, conv="web-1-rp-hist")
    spy.calls.clear()
    replay.replay(get_db(), fb)
    router_input = spy.calls[0][1][-1].content
    assert "Can I carry over my leave?" in router_input and "Up to five days" in router_input                      # the earlier question and answer


def test_the_replay_uses_the_prompt_versions_of_the_original_answer(world, spy):
    fb = rated(world, conv="web-1-rp-prompt")
    with get_db().session() as s:
        pinned = {p.scope: p.id for p in s.scalars(select(Prompt).where(Prompt.status == "Published"))}
    new = prompts.save_draft(get_db(), prompts.BASE_ANSWER, 0, rag.SYSTEM_PROMPT + "\n\nNEW-RULE-AFTER-THE-ANSWER", "t")
    prompts.publish(get_db(), new["id"], "t")
    try:
        spy.calls.clear()
        replay.replay(get_db(), fb)
        system = next(c[1][0].content for c in spy.calls if c[0] == "stream")
        assert "NEW-RULE-AFTER-THE-ANSWER" not in system and "Channel Agnostic Agent" in system
        assert pinned                                                                                                # a version was in force at the time
    finally:
        prompts.rollback(get_db(), pinned[prompts.BASE_ANSWER], "t")


def test_a_change_in_the_settings_is_reported_so_a_reviewer_can_see_why_the_answer_changed(world, spy):
    from app import scoped_settings
    fb = rated(world, conv="web-1-rp-settings")
    assert replay.replay(get_db(), fb)["settings_changed"] == []
    scoped_settings.put("kb", world.kb["id"], {"score_threshold": 0.31}, "t", world.db)
    assert "retrieval" in replay.replay(get_db(), fb)["settings_changed"]


@pytest.mark.parametrize("what", ["document_gone", "document_changed", "access_lost", "trace_missing", "prompt_gone", "rater_unavailable"])
def test_when_a_fair_replay_is_not_possible_no_model_is_called(world, spy, monkeypatch, what):
    fb = rated(world, conv="web-1-rp-" + what)
    with get_db().session() as s:
        doc = s.scalar(select(Document).where(Document.kb_id == world.kb["id"]))
        doc_id, original = doc.id, (doc.visibility, doc.updated_at)
        if what == "document_changed":
            doc.updated_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        if what == "access_lost":
            s.execute(update(Document).where(Document.id == doc_id).values(visibility="groups", updated_at=original[1]))   # hidden; the time stays, to test access alone
        if what == "trace_missing":
            s.delete(s.get(AnswerTrace, fb.answer_id))
        if what == "prompt_gone":
            t = s.get(AnswerTrace, fb.answer_id)
            t.prompt_versions = {"base_answer": {"id": 999999, "version": 1}}
    if what == "rater_unavailable":
        monkeypatch.setattr(replay, "rater", lambda db, f: None)
    try:
        spy.purposes.clear()
        ok, code = replay.possible(get_db(), fb) if what != "document_gone" else (None, None)
        if what == "document_gone":
            with get_db().session() as s:
                t = s.get(AnswerTrace, fb.answer_id)
                t.hits = [{**h, "doc_id": 424242} for h in t.hits]
            ok, code = replay.possible(get_db(), fb)
        assert (ok, code) == (False, what)
        v = replay.verify(get_db(), fb)
        assert v["outcome"] == "not_possible" and v["reason"] == what and v["model_called"] is False and spy.purposes == []
    finally:
        with get_db().session() as s:
            d = s.get(Document, doc_id)
            d.visibility, d.updated_at = original


def test_a_raters_lost_access_never_leaks_the_restricted_document_into_a_replay(world, spy):
    """Adversarial: even if the replay were forced, a rater who cannot see the document gets nothing from it."""
    fb = rated(world, conv="web-1-rp-leak")
    with get_db().session() as s:
        doc = s.scalar(select(Document).where(Document.kb_id == world.kb["id"]))
        doc_id, original = doc.id, doc.visibility
        doc.visibility = "groups"
    try:
        out = replay.replay(get_db(), fb)
        assert "five days" not in out["answer"].lower() and out["found"] is False
        v = replay.verify(get_db(), fb)
        assert "five days" not in json.dumps(v).lower() and v["outcome"] == "not_possible"
    finally:
        with get_db().session() as s:
            s.get(Document, doc_id).visibility = original


def test_the_stored_replay_answer_is_masked(world, spy):
    spy.parts = ["Write to rita.k@example.net, up to five days [1]."]
    fb = rated(world, conv="web-1-rp-mask")
    out = replay.replay(get_db(), fb)
    assert "rita.k@example.net" not in out["answer"]


def test_the_whole_path_from_a_rating_to_a_ticket_with_the_real_replay(world, spy, monkeypatch):
    monkeypatch.setattr(ticket_agent, "title", lambda item: None)
    monkeypatch.setattr(ticket_agent, "same_issue", lambda item, cands: set())
    intent = types.SimpleNamespace(invoke=lambda msgs, **kw: types.SimpleNamespace(content=json.dumps({"final": {"intent": "negative", "summary": "The carry-over limit seems wrong."}})))
    monkeypatch.setattr(agent_runtime, "get_chat_model", lambda: intent)
    global_settings.update({"feedback_processing_mode": "auto"}, "t", get_db())
    fb = rated(world, conv="web-1-rp-e2e")
    assert feedback_flow.process(get_db(), fb.id) == "ticketed"
    row = fresh(fb)
    assert row.verification["outcome"] == "confirmed" and row.verification["settings_changed"] == [] and row.ticket_id
    with get_db().session() as s:
        ev = s.get(Ticket, row.ticket_id).evidence[0]
    assert ev["replay"]["outcome"] == "confirmed" and ev["summary"] == "The carry-over limit seems wrong."
