"""Tickets (Release 3, Phase 2; D55, D56, D63, D67): the state machine, the event history and who may do what.

Every change to a ticket goes through this module, one function each, so the rules cannot be bypassed:

* `transition()` is the only way a status changes. It checks the table of allowed moves (A3) and who may make them, changes the status
  with a compare-and-set (so exactly one of two runners claiming the same ticket wins) and writes the event in the same transaction.
* `reassign()` and `comment()` write non-state events. `add_evidence()` (in ticketing.py) writes `evidence_added`. None of these change
  `tickets.status`, which always equals the latest STATE event.
* Events are never updated. Retention (ticketing.retire) removes them only together with their closed ticket.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.db import Database, Ticket, TicketEvent, TicketType, get_db

log = logging.getLogger(__name__)

NEW, ASSIGNED, IN_PROGRESS, ASSIGNED_TO_HUMAN, HOLD, COMPLETED, DISMISSED = (
    "new", "assigned", "in_progress", "assigned_to_human", "hold", "completed", "dismissed")
STATUSES = (NEW, ASSIGNED, IN_PROGRESS, ASSIGNED_TO_HUMAN, HOLD, COMPLETED, DISMISSED)
CLOSED = (COMPLETED, DISMISSED)
OPEN = tuple(s for s in STATUSES if s not in CLOSED)
EVIDENCE_ADDED, REASSIGNED, COMMENT = "evidence_added", "reassigned", "comment"
OTHER_EVENTS = (EVIDENCE_ADDED, REASSIGNED, COMMENT)
AUTONOMOUS, USER, GROUP = "autonomous", "user", "group"

# (from, to) -> who may make the move. system = the pipeline's own rules (assignment, the release of the queue, the sweeper);
# agent = the admin agent; manager = an Admin, or an Editor of the ticket's department; worker = the person it is assigned to
# (or a group member), or a manager.
TRANSITIONS = {
    (NEW, ASSIGNED): ("system",),
    (NEW, ASSIGNED_TO_HUMAN): ("system",),
    (ASSIGNED, IN_PROGRESS): ("agent",),                          # the claim
    (ASSIGNED, ASSIGNED_TO_HUMAN): ("system", "manager"),         # a reassign, or the release of the queue (D63)
    (IN_PROGRESS, COMPLETED): ("agent", "worker"),
    (IN_PROGRESS, DISMISSED): ("agent", "worker"),
    (IN_PROGRESS, ASSIGNED_TO_HUMAN): ("agent", "system"),        # cannot resolve, approval needed, or the lease expired
    (IN_PROGRESS, HOLD): ("worker",),
    (ASSIGNED_TO_HUMAN, IN_PROGRESS): ("worker",),
    (ASSIGNED_TO_HUMAN, HOLD): ("worker",),
    (HOLD, IN_PROGRESS): ("worker",),
}


class TicketError(Exception):
    """A request the caller got wrong or may not make. `status` is the HTTP code an API should answer with."""
    status = 400


class NotFound(TicketError):
    status = 404


class NotAllowed(TicketError):
    status = 403


class IllegalTransition(TicketError):
    status = 409


@dataclass(frozen=True)
class Actor:
    kind: str                         # system | agent | person
    user_id: int | None = None
    role: str = ""                    # a person's panel role: admin | editor | viewer
    departments: tuple = ()           # departments a person edits
    group_ids: tuple = ()             # groups a person belongs to

    @property
    def event_type(self) -> str:
        return {"system": "system", "agent": "autonomous"}.get(self.kind, "manual")

    @staticmethod
    def system() -> "Actor":
        return Actor("system")

    @staticmethod
    def agent() -> "Actor":
        return Actor("agent")

    @staticmethod
    def person(who) -> "Actor":
        """From an identity (app/identity.py)."""
        return Actor("person", getattr(who, "user_id", None), getattr(who, "role", ""), tuple(getattr(who, "edits_departments", ()) or ()),
                     tuple(getattr(who, "group_ids", ()) or ()))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_manager(actor: Actor, ticket: Ticket) -> bool:
    return actor.kind == "person" and (actor.role == "admin" or (actor.role == "editor" and ticket.department_id in actor.departments))


def is_worker(actor: Actor, ticket: Ticket) -> bool:
    if is_manager(actor, ticket):
        return True
    if actor.kind != "person" or actor.user_id is None:
        return False
    if ticket.assignee_type == USER:
        return ticket.assignee_id == actor.user_id
    if ticket.assignee_type == GROUP:
        return ticket.assignee_id in actor.group_ids
    return False


def may(actor: Actor, who: str, ticket: Ticket) -> bool:
    if who in ("system", "agent"):
        return actor.kind == who
    return {"manager": is_manager, "worker": is_worker}[who](actor, ticket)


def can_see(actor: Actor, ticket: Ticket) -> bool:
    """An Admin sees every ticket, an Editor the tickets of the departments they edit, the people they are assigned to or a group member
    see their own. Nobody else sees any."""
    return is_worker(actor, ticket)


def view_scope(q, actor: Actor):
    """Limit a query on Ticket to what `actor` may see."""
    if actor.role == "admin":
        return q
    from sqlalchemy import and_, or_
    parts = []
    if actor.departments:
        parts.append(Ticket.department_id.in_(actor.departments))
    if actor.user_id is not None:
        parts.append(and_(Ticket.assignee_type == USER, Ticket.assignee_id == actor.user_id))
    if actor.group_ids:
        parts.append(and_(Ticket.assignee_type == GROUP, Ticket.assignee_id.in_(actor.group_ids)))
    return q.where(or_(*parts)) if parts else q.where(Ticket.id < 0)


def _event(s, ticket_id: int, event: str, actor: Actor, note: str = "", detail: dict | None = None, now: datetime | None = None) -> TicketEvent:
    row = TicketEvent(ticket_id=ticket_id, event=event, actor_type=actor.event_type, actor_id=actor.user_id if actor.kind == "person" else None,
                      note=(note or "")[:2000], detail=detail or {}, created_at=now or _now())
    s.add(row)
    return row


def create(s, *, type_name: str, ref_id: int | None, kb_id: int | None, department_id: int | None, title: str, evidence: list,
           raised_by: int | None, related_ticket_id: int | None = None, actor: Actor | None = None) -> Ticket:
    """A new ticket in status `new`, with its first event. `s` is an open session; the caller assigns it next (assign_*)."""
    actor = actor or Actor.system()
    kind = s.scalar(select(TicketType).where(TicketType.name == type_name))
    if kind is None:
        raise TicketError(f"Unknown ticket type: {type_name}.")
    now = _now()
    t = Ticket(type_id=kind.id, ref_id=ref_id, kb_id=kb_id, department_id=department_id, title=title[:200], evidence=evidence, status=NEW,
               raised_by=raised_by, related_ticket_id=related_ticket_id, created_at=now, updated_at=now)
    s.add(t)
    s.flush()
    _event(s, t.id, NEW, actor, detail={"related_ticket_id": related_ticket_id} if related_ticket_id else {}, now=now)
    return t


def transition(db: Database | None, ticket_id: int, to: str, actor: Actor, *, note: str = "", detail: dict | None = None,
               expect: str | None = None, assignee: tuple[str, int | None] | None = None) -> dict:
    """Move a ticket to `to`. Raises NotFound, IllegalTransition (the move is not in the table, or the ticket is no longer in the status
    the caller expected: another runner got there first) or NotAllowed (the actor may not make this move). `assignee` = (type, id) sets
    the assignee in the same step (an assignment, a reassign, a person taking a ticket). Returns the ticket as a dict."""
    db = db or get_db()
    if to not in STATUSES:
        raise IllegalTransition(f"Unknown status: {to}.")
    with db.session() as s:
        t = s.get(Ticket, ticket_id)
        if t is None:
            raise NotFound("No such ticket.")
        frm = t.status
        if expect is not None and frm != expect:
            raise IllegalTransition(f"The ticket is {frm}, not {expect}.")
        who = TRANSITIONS.get((frm, to))
        if who is None:
            raise IllegalTransition(f"A ticket cannot go from {frm} to {to}.")
        if not any(may(actor, w, t) for w in who):
            raise NotAllowed("You may not make this change.")
        values = {"status": to, "updated_at": _now()}
        if to in CLOSED:
            values["closed_at"] = _now()
        if assignee is not None:
            values["assignee_type"], values["assignee_id"] = assignee[0], assignee[1]
        elif to == IN_PROGRESS and actor.kind == "person" and t.assignee_type != USER:
            values["assignee_type"], values["assignee_id"] = USER, actor.user_id          # a person who takes a ticket owns it
        elif to == IN_PROGRESS and actor.kind == "agent":
            values["assignee_type"], values["assignee_id"] = AUTONOMOUS, None
        # compare-and-set: of two runners that read the same status, only one changes a row
        n = s.execute(update(Ticket).where(Ticket.id == ticket_id, Ticket.status == frm).values(**values)).rowcount
        if n != 1:
            raise IllegalTransition("The ticket was changed by someone else. Reload it and try again.")
        _event(s, ticket_id, to, actor, note, {**(detail or {}), "from": frm})
        s.flush()
        s.refresh(t)
        return as_dict(t)


def reassign(db: Database | None, ticket_id: int, to_type: str, to_id: int | None, actor: Actor, note: str = "") -> dict:
    """Give a ticket to a person or a group. From `assigned` (the agent's queue) this is the move to `assigned_to_human`; from
    `assigned_to_human`, `hold` or a person's `in_progress` it only changes the assignee (a `reassigned` event, no status change)."""
    db = db or get_db()
    if to_type not in (USER, GROUP) or to_id is None:
        raise TicketError("Choose a person or a group.")
    with db.session() as s:
        t = s.get(Ticket, ticket_id)
        if t is None:
            raise NotFound("No such ticket.")
        status, kind = t.status, t.assignee_type
    if status == ASSIGNED:
        return transition(db, ticket_id, ASSIGNED_TO_HUMAN, actor, note=note, assignee=(to_type, to_id), expect=ASSIGNED)
    if status in CLOSED or status == NEW or (status == IN_PROGRESS and kind == AUTONOMOUS):
        raise IllegalTransition(f"A ticket that is {status} cannot be reassigned.")
    with db.session() as s:
        t = s.get(Ticket, ticket_id)
        if not is_manager(actor, t):
            raise NotAllowed("Only an Admin, or an Editor of the department, can reassign a ticket.")
        before = (t.assignee_type, t.assignee_id)
        t.assignee_type, t.assignee_id, t.updated_at = to_type, to_id, _now()
        _event(s, ticket_id, REASSIGNED, actor, note, {"from": list(before), "to": [to_type, to_id]})
        s.flush()
        s.refresh(t)
        return as_dict(t)


def comment(db: Database | None, ticket_id: int, actor: Actor, note: str) -> dict:
    """A note on the timeline. Anyone who may see the ticket may add one."""
    db = db or get_db()
    note = (note or "").strip()
    if not note:
        raise TicketError("Write a note first.")
    with db.session() as s:
        t = s.get(Ticket, ticket_id)
        if t is None or not (can_see(actor, t) or actor.kind in ("system", "agent")):
            raise NotFound("No such ticket.")
        _event(s, ticket_id, COMMENT, actor, note)
        t.updated_at = _now()
        s.flush()
        return as_dict(t)


# ---- reading -----------------------------------------------------------------------------------------------------------------------
def as_dict(t: Ticket, type_name: str = "") -> dict:
    return {"id": t.id, "type_id": t.type_id, "type": type_name, "ref_id": t.ref_id, "kb_id": t.kb_id, "department_id": t.department_id,
            "category": t.category, "title": t.title, "status": t.status, "assignee_type": t.assignee_type, "assignee_id": t.assignee_id,
            "raised_by": t.raised_by, "related_ticket_id": t.related_ticket_id, "evidence_count": len(t.evidence or []),
            "created_at": t.created_at, "updated_at": t.updated_at, "closed_at": t.closed_at}


def events(db: Database | None, ticket_id: int) -> list[dict]:
    with (db or get_db()).session() as s:
        return [{"id": e.id, "event": e.event, "actor_type": e.actor_type, "actor_id": e.actor_id, "note": e.note, "detail": e.detail,
                 "created_at": e.created_at} for e in s.scalars(select(TicketEvent).where(TicketEvent.ticket_id == ticket_id).order_by(TicketEvent.id))]


def status_matches_events(db: Database | None, ticket_id: int) -> bool:
    """`tickets.status` equals the latest state event (D56). Used by tests and the integrity report."""
    with (db or get_db()).session() as s:
        t = s.get(Ticket, ticket_id)
        last = s.scalars(select(TicketEvent).where(TicketEvent.ticket_id == ticket_id, TicketEvent.event.in_(STATUSES)).order_by(TicketEvent.id.desc())).first()
        return t is not None and last is not None and t.status == last.event
