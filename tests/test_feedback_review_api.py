"""Release 3, Phase 3: the review queue, the decisions and the processing report in the admin API (D54, A8)."""
import types
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app import feedback_flow, global_settings, scoped_settings, ticket_agent
from app.db import Department, DepartmentEditor, Feedback, KnowledgeBase, Ticket, TicketEvent, get_db
from tests.test_feedback_capture import ask, rate, setup, world  # noqa: F401
from tests.test_release2_suggestions import P, person, sign_in
from tests.test_ticketing import make_feedback  # noqa: F401

R = P + "/feedback"


@pytest.fixture(autouse=True)
def _patches(monkeypatch):
    monkeypatch.setattr(ticket_agent, "title", lambda item: None)
    monkeypatch.setattr(ticket_agent, "same_issue", lambda item, cands: set())
    yield
    with get_db().session() as s:
        s.execute(delete(TicketEvent))
        s.execute(delete(Ticket))
    global_settings.update({"feedback_processing_mode": "off", "feedback_review_age_days": 7}, "t", get_db())


@pytest.fixture(scope="module")
def staff(world):
    db = get_db()
    with db.session() as s:
        s.add(Department(name="RvIT", retention_days=30, banned_topics="", is_default=False))
    with db.session() as s:
        other = s.scalar(select(Department.id).where(Department.name == "RvIT"))
    ed = person(db, "rv-ed@sysfore.com", "editor", world.hr)
    ed2 = person(db, "rv-ed2@sysfore.com", "editor", other)
    with db.session() as s:
        s.add(DepartmentEditor(department_id=world.hr, user_id=ed["id"]))
        s.add(DepartmentEditor(department_id=other, user_id=ed2["id"]))
    editor, eh = sign_in("rv-ed@sysfore.com")
    editor2, eh2 = sign_in("rv-ed2@sysfore.com")
    return types.SimpleNamespace(editor=editor, eh=eh, editor2=editor2, eh2=eh2, other=other)


def waiting(world, comment="The limit is wrong", age_days=0, verification=None, intent="negative"):
    r = ask(world, conv=f"web-1-rv-{datetime.now().timestamp()}")
    rate(world, r, "down", comment=comment)
    with get_db().session() as s:
        fb = s.scalar(select(Feedback).where(Feedback.answer_id == r["answer_id"]))
        fb.state, fb.review_reason, fb.intent, fb.summary = "human_review", "review_all", intent, "The carry-over limit seems wrong."
        fb.verification = verification or {"outcome": "confirmed", "reason": "Same figure.", "replay_answer": "Up to five days.", "settings_changed": ["model"], "model_called": True,
                                           "replayed_at": datetime.now(timezone.utc).isoformat()}
        fb.updated_at = datetime.now(timezone.utc) - timedelta(days=age_days)
        return fb.id


def test_the_queue_shows_what_a_reviewer_needs_side_by_side_and_marks_old_items(world, staff):
    fresh, old = waiting(world), waiting(world, age_days=9)
    q = world.admin.get(R + "/review", headers=world.ah).json()
    assert q["total"] == 2 and q["age_days"] == 7
    item = next(i for i in q["items"] if i["id"] == old)
    assert item["old"] is True and next(i for i in q["items"] if i["id"] == fresh)["old"] is False
    assert item["question"] and item["answer"] and item["comment"] == "The limit is wrong" and item["summary"].startswith("The carry-over")
    assert item["verification"]["outcome"] == "confirmed" and item["verification"]["replay_answer"] == "Up to five days." and item["verification"]["settings_changed"] == ["model"]
    assert q["items"][0]["id"] == old                                                  # the oldest first


def test_an_editor_reviews_only_their_own_departments_items(world, staff):
    fid = waiting(world)
    assert staff.editor.get(R + "/review", headers=staff.eh).json()["total"] == 1
    assert staff.editor2.get(R + "/review", headers=staff.eh2).json()["total"] == 0
    assert staff.editor2.post(f"{R}/review/{fid}/claim", headers=staff.eh2).status_code == 404
    assert staff.editor2.post(f"{R}/review/{fid}/decide", headers=staff.eh2, json={"decision": "negative"}).status_code == 404
    assert staff.editor.post(f"{R}/review/{fid}/claim", headers=staff.eh).json() == {"claimed": True, "held_by": staff.editor.get(P + "/me", headers=staff.eh).json().get("held", "user:%d" % _uid("rv-ed@sysfore.com"))}


def _uid(email):
    from app import identity
    return identity.resolve(email, get_db()).user_id


def test_two_people_cannot_decide_one_item_and_the_decision_is_stored_and_audited(world, staff):
    fid = waiting(world)
    assert staff.editor.post(f"{R}/review/{fid}/claim", headers=staff.eh).json()["claimed"] is True
    assert world.admin.post(f"{R}/review/{fid}/claim", headers=world.ah).json()["claimed"] is False
    assert world.admin.post(f"{R}/review/{fid}/decide", headers=world.ah, json={"decision": "positive"}).status_code == 409           # held by the editor
    assert world.admin.get(R + "/review", headers=world.ah).json()["items"][0]["held_by"] == f"user:{_uid('rv-ed@sysfore.com')}"
    done = staff.editor.post(f"{R}/review/{fid}/decide", headers=staff.eh, json={"decision": "negative", "note": "Real. Mail lee.k@example.net"})
    assert done.status_code == 200 and done.json()["state"] == "ticketed"
    with get_db().session() as s:
        fb = s.get(Feedback, fid)
        assert fb.ticket_id and fb.reviewer_id == _uid("rv-ed@sysfore.com") and "lee.k@example.net" not in fb.review_note
    assert world.admin.post(f"{R}/review/{fid}/decide", headers=world.ah, json={"decision": "positive"}).status_code == 409           # too late
    from app.channels import get_store
    assert any(a["action"] == "feedback.review" for a in get_store().recent_audit(20))
    assert world.admin.post(f"{R}/review/{fid}/decide", headers=world.ah, json={"decision": "maybe"}).status_code in (409, 422)


def test_the_report_counts_the_funnel_and_how_often_reviewers_overturn_the_replay(world, staff):
    agree = waiting(world)                                                                # replay confirmed, reviewer negative: agreed
    overturned = waiting(world)                                                           # replay confirmed, reviewer positive: overturned
    failed = waiting(world, verification={"outcome": "failed", "reason": "Fixed now.", "model_called": True, "replayed_at": datetime.now(timezone.utc).isoformat()})
    for fid, decision in ((agree, "negative"), (overturned, "positive"), (failed, "negative")):        # the last: replay failed, reviewer negative: overturned
        feedback_flow.decide(get_db(), fid, 1, decision)
    rep = world.admin.get(R + "/processing-report", headers=world.ah).json()
    assert rep["reviewed_replays"] == 3 and rep["overturned"] == 2 and rep["overturned_share"] == round(2 / 3, 3)
    assert rep["replay_outcomes"]["confirmed"] == 2 and rep["replay_outcomes"]["failed"] == 1 and rep["funnel"]["ticketed"] == 2 and rep["funnel"]["no_ticket"] >= 1
    assert staff.editor.get(R + "/processing-report", headers=staff.eh).status_code == 403            # Admins only
    assert "The limit is wrong" not in str(rep)


def test_the_new_settings_are_validated_and_the_mode_can_be_set_per_knowledge_base(world, staff):
    put = lambda values: world.admin.put(P + "/settings", headers=world.ah, json={"values": values})
    assert put({"feedback_processing_mode": "review_all", "feedback_replay_daily_cap": 10, "feedback_retry_limit": 2}).status_code == 200
    assert put({"feedback_processing_mode": "everything"}).status_code == 422
    assert put({"feedback_retry_limit": 0}).status_code == 422 and put({"feedback_agent_max_steps": 99}).status_code == 422
    assert put({"feedback_intent_enabled": "yes"}).status_code == 422
    assert global_settings.current(get_db()).feedback_processing_mode == "review_all"
    scoped_settings.put("kb", world.kb["id"], {"feedback_processing_mode": "auto"}, "t", get_db())
    assert scoped_settings.get("feedback_processing_mode", kb_id=world.kb["id"], db=get_db()) == "auto"
    with pytest.raises(ValueError):
        scoped_settings.put("channel", "web", {"feedback_processing_mode": "auto"}, "t", get_db())             # not allowed at that scope
    scoped_settings.invalidate()
    global_settings.update({"feedback_processing_mode": "off", "feedback_replay_daily_cap": 50, "feedback_retry_limit": 3}, "t", get_db())
