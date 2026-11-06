"""Release 3, Phase 2: the ticket state machine, its events and who may do what (D56, D63, A3)."""
import threading

import pytest
from sqlalchemy import select, update

from app import tickets
from app.db import Ticket, TicketEvent, TicketType, get_db
from app.tickets import (ASSIGNED, ASSIGNED_TO_HUMAN, COMPLETED, DISMISSED, HOLD, IN_PROGRESS, NEW, STATUSES, Actor, IllegalTransition, NotAllowed,
                         NotFound, TicketError)

ADMIN = Actor("person", user_id=1, role="admin")
EDITOR_HR = Actor("person", user_id=2, role="editor", departments=(10,))
EDITOR_IT = Actor("person", user_id=3, role="editor", departments=(20,))
VIEWER = Actor("person", user_id=4, role="viewer")
ALICE = Actor("person", user_id=5, role="viewer", group_ids=(7,))                  # a member of group 7
SYSTEM, AGENT = Actor.system(), Actor.agent()


@pytest.fixture
def db():
    return get_db()


def new_ticket(db, department_id=10, status=NEW, assignee=None):
    with db.session() as s:
        t = tickets.create(s, type_name="feedback", ref_id=1, kb_id=1, department_id=department_id, title="A ticket", evidence=[], raised_by=99)
        tid = t.id
    if status != NEW:
        walk(db, tid, status, assignee)
    return tid


def walk(db, tid, status, assignee=None):
    """Move a new ticket to `status` along the allowed paths."""
    if status == ASSIGNED:
        tickets.transition(db, tid, ASSIGNED, SYSTEM, assignee=(tickets.AUTONOMOUS, None))
    elif status == ASSIGNED_TO_HUMAN:
        tickets.transition(db, tid, ASSIGNED_TO_HUMAN, SYSTEM, assignee=assignee or (tickets.USER, 5))
    elif status == IN_PROGRESS:
        tickets.transition(db, tid, ASSIGNED, SYSTEM, assignee=(tickets.AUTONOMOUS, None))
        tickets.transition(db, tid, IN_PROGRESS, AGENT)
    elif status == HOLD:
        walk(db, tid, ASSIGNED_TO_HUMAN, assignee)
        tickets.transition(db, tid, HOLD, ADMIN)
    elif status in (COMPLETED, DISMISSED):
        walk(db, tid, IN_PROGRESS)
        tickets.transition(db, tid, status, AGENT)


def get(db, tid):
    with db.session() as s:
        return s.get(Ticket, tid)


def test_a_new_ticket_has_status_new_and_a_first_event(db):
    tid = new_ticket(db)
    assert get(db, tid).status == NEW and [e["event"] for e in tickets.events(db, tid)] == [NEW]
    assert tickets.status_matches_events(db, tid)


def test_the_feedback_ticket_type_is_seeded(db):
    with db.session() as s:
        assert s.scalar(select(TicketType.name).where(TicketType.name == "feedback")) == "feedback"
    with db.session() as s, pytest.raises(TicketError):
        tickets.create(s, type_name="nonsense", ref_id=None, kb_id=None, department_id=None, title="x", evidence=[], raised_by=None)


ALLOWED = [(frm, to) for (frm, to) in tickets.TRANSITIONS]


@pytest.mark.parametrize("frm,to", [(a, b) for a in STATUSES for b in STATUSES if (a, b) not in tickets.TRANSITIONS])
def test_a_move_that_is_not_in_the_table_is_rejected_and_writes_no_event(db, frm, to):
    tid = new_ticket(db)
    walk_to = {NEW: None, ASSIGNED: ASSIGNED, IN_PROGRESS: IN_PROGRESS, ASSIGNED_TO_HUMAN: ASSIGNED_TO_HUMAN, HOLD: HOLD, COMPLETED: COMPLETED, DISMISSED: DISMISSED}[frm]
    if walk_to:
        walk(db, tid, walk_to)
    before = len(tickets.events(db, tid))
    with pytest.raises(IllegalTransition):
        tickets.transition(db, tid, to, ADMIN)
    assert len(tickets.events(db, tid)) == before and get(db, tid).status == frm


def test_the_whole_table_is_what_the_plan_says():
    assert set(tickets.TRANSITIONS) == {
        (NEW, ASSIGNED), (NEW, ASSIGNED_TO_HUMAN), (ASSIGNED, IN_PROGRESS), (ASSIGNED, ASSIGNED_TO_HUMAN), (IN_PROGRESS, COMPLETED),
        (IN_PROGRESS, DISMISSED), (IN_PROGRESS, ASSIGNED_TO_HUMAN), (IN_PROGRESS, HOLD), (ASSIGNED_TO_HUMAN, IN_PROGRESS),
        (ASSIGNED_TO_HUMAN, HOLD), (HOLD, IN_PROGRESS)}
    assert tickets.CLOSED == (COMPLETED, DISMISSED) and not [k for k in tickets.TRANSITIONS if k[0] in tickets.CLOSED]       # a closed ticket stays closed


def test_status_equals_the_latest_state_event_after_every_path(db):
    for target in (ASSIGNED, ASSIGNED_TO_HUMAN, IN_PROGRESS, HOLD, COMPLETED, DISMISSED):
        tid = new_ticket(db, status=target)
        assert get(db, tid).status == target and tickets.status_matches_events(db, tid), target


def test_only_the_system_assigns_a_new_ticket_and_only_the_agent_claims(db):
    tid = new_ticket(db)
    for actor in (ADMIN, EDITOR_HR, AGENT):
        with pytest.raises(NotAllowed):
            tickets.transition(db, tid, ASSIGNED, actor)
    tickets.transition(db, tid, ASSIGNED, SYSTEM, assignee=(tickets.AUTONOMOUS, None))
    for actor in (ADMIN, SYSTEM, EDITOR_HR):
        with pytest.raises(NotAllowed):
            tickets.transition(db, tid, IN_PROGRESS, actor)                  # the claim belongs to the agent
    out = tickets.transition(db, tid, IN_PROGRESS, AGENT)
    assert out["status"] == IN_PROGRESS and out["assignee_type"] == tickets.AUTONOMOUS
    ev = tickets.events(db, tid)[-1]
    assert ev["actor_type"] == "autonomous" and ev["actor_id"] is None and ev["detail"]["from"] == ASSIGNED


def test_two_runners_claiming_the_same_ticket_exactly_one_wins(db):
    tid = new_ticket(db, status=ASSIGNED)
    results, barrier = [], threading.Barrier(8)

    def run():
        barrier.wait()
        try:
            tickets.transition(db, tid, IN_PROGRESS, AGENT, expect=ASSIGNED)
            results.append("won")
        except IllegalTransition:
            results.append("lost")
    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("won") == 1 and results.count("lost") == 7
    assert len([e for e in tickets.events(db, tid) if e["event"] == IN_PROGRESS]) == 1 and tickets.status_matches_events(db, tid)


def test_the_compare_and_set_refuses_a_stale_move(db):
    """The status changes between the check and the update: the update changes no row and nothing is written."""
    tid = new_ticket(db, status=ASSIGNED)
    real = tickets.update

    def sneaky(model):
        with db.session() as s:                                              # someone else moves it first
            s.execute(real(Ticket).where(Ticket.id == tid).values(status=IN_PROGRESS))
        return real(model)
    tickets.update = sneaky
    try:
        with pytest.raises(IllegalTransition):
            tickets.transition(db, tid, IN_PROGRESS, AGENT)
    finally:
        tickets.update = real
    assert [e["event"] for e in tickets.events(db, tid)] == [NEW, ASSIGNED]


def test_expect_guards_against_a_changed_status(db):
    tid = new_ticket(db, status=IN_PROGRESS)
    with pytest.raises(IllegalTransition):
        tickets.transition(db, tid, IN_PROGRESS, AGENT, expect=ASSIGNED)


def test_the_assignee_works_the_ticket_and_others_cannot(db):
    tid = new_ticket(db, status=ASSIGNED_TO_HUMAN, assignee=(tickets.USER, ALICE.user_id))
    for stranger in (VIEWER, EDITOR_IT):
        with pytest.raises(NotAllowed):
            tickets.transition(db, tid, IN_PROGRESS, stranger)
    out = tickets.transition(db, tid, IN_PROGRESS, ALICE, note="On it")
    assert out["status"] == IN_PROGRESS and out["assignee_type"] == tickets.USER and out["assignee_id"] == ALICE.user_id
    assert tickets.transition(db, tid, HOLD, ALICE)["status"] == HOLD
    assert tickets.transition(db, tid, IN_PROGRESS, ALICE)["status"] == IN_PROGRESS
    assert tickets.transition(db, tid, COMPLETED, ALICE, note="Fixed")["status"] == COMPLETED
    assert get(db, tid).closed_at is not None and tickets.status_matches_events(db, tid)
    with pytest.raises(IllegalTransition):
        tickets.transition(db, tid, IN_PROGRESS, ADMIN)                         # closed stays closed


def test_a_group_member_and_a_department_editor_can_work_a_ticket(db):
    tid = new_ticket(db, status=ASSIGNED_TO_HUMAN, assignee=(tickets.GROUP, 7))
    assert tickets.transition(db, tid, IN_PROGRESS, ALICE)["assignee_type"] == tickets.USER        # taking it makes it theirs
    tid2 = new_ticket(db, status=ASSIGNED_TO_HUMAN, assignee=(tickets.USER, 99))
    assert tickets.transition(db, tid2, IN_PROGRESS, EDITOR_HR)["status"] == IN_PROGRESS
    tid3 = new_ticket(db, status=ASSIGNED_TO_HUMAN, assignee=(tickets.USER, 99))
    with pytest.raises(NotAllowed):
        tickets.transition(db, tid3, IN_PROGRESS, EDITOR_IT)                    # another department


def test_a_person_cannot_finish_the_agents_ticket_but_the_agent_can_hand_it_over(db):
    tid = new_ticket(db, status=IN_PROGRESS)
    with pytest.raises(NotAllowed):
        tickets.transition(db, tid, COMPLETED, VIEWER)
    out = tickets.transition(db, tid, ASSIGNED_TO_HUMAN, AGENT, note="No valid fix", assignee=(tickets.GROUP, 7), detail={"diagnosis": "none"})
    assert out["status"] == ASSIGNED_TO_HUMAN and tickets.events(db, tid)[-1]["detail"]["diagnosis"] == "none"


def test_reassign_from_the_agents_queue_is_a_state_change_by_a_manager(db):
    tid = new_ticket(db, status=ASSIGNED)
    with pytest.raises(NotAllowed):
        tickets.reassign(db, tid, tickets.USER, 5, VIEWER)
    out = tickets.reassign(db, tid, tickets.USER, 5, ADMIN, "to Alice")
    assert out["status"] == ASSIGNED_TO_HUMAN and out["assignee_id"] == 5 and tickets.status_matches_events(db, tid)


def test_reassign_between_people_writes_an_event_but_no_status_change(db):
    tid = new_ticket(db, status=ASSIGNED_TO_HUMAN, assignee=(tickets.USER, 5))
    with pytest.raises(NotAllowed):
        tickets.reassign(db, tid, tickets.USER, 6, ALICE)                       # the assignee is not a manager
    out = tickets.reassign(db, tid, tickets.GROUP, 7, EDITOR_HR, "to the group")
    assert out["status"] == ASSIGNED_TO_HUMAN and (out["assignee_type"], out["assignee_id"]) == (tickets.GROUP, 7)
    last = tickets.events(db, tid)[-1]
    assert last["event"] == tickets.REASSIGNED and last["detail"]["from"] == [tickets.USER, 5] and tickets.status_matches_events(db, tid)
    with pytest.raises(TicketError):
        tickets.reassign(db, tid, "robot", 1, ADMIN)
    done = new_ticket(db, status=COMPLETED)
    with pytest.raises(IllegalTransition):
        tickets.reassign(db, done, tickets.USER, 5, ADMIN)


def test_a_comment_does_not_change_the_status(db):
    tid = new_ticket(db, status=ASSIGNED_TO_HUMAN, assignee=(tickets.USER, 5))
    tickets.comment(db, tid, ALICE, "Looking at it")
    assert tickets.events(db, tid)[-1]["event"] == tickets.COMMENT and get(db, tid).status == ASSIGNED_TO_HUMAN and tickets.status_matches_events(db, tid)
    with pytest.raises(NotFound):
        tickets.comment(db, tid, VIEWER, "Hello")
    with pytest.raises(TicketError):
        tickets.comment(db, tid, ALICE, "   ")


def test_who_can_see_which_tickets(db):
    mine = new_ticket(db, department_id=10, status=ASSIGNED_TO_HUMAN, assignee=(tickets.USER, ALICE.user_id))
    other = new_ticket(db, department_id=20, status=ASSIGNED_TO_HUMAN, assignee=(tickets.USER, 99))

    def visible(actor):
        with db.session() as s:
            return {t.id for t in s.scalars(tickets.view_scope(select(Ticket), actor)) if t.id in (mine, other)}
    assert visible(ADMIN) == {mine, other} and visible(EDITOR_HR) == {mine} and visible(EDITOR_IT) == {other}
    assert visible(ALICE) == {mine} and visible(VIEWER) == set()


def test_events_are_never_updated_by_this_module():
    import inspect
    source = inspect.getsource(tickets)
    assert "update(TicketEvent" not in source and "delete(TicketEvent" not in source
