"""The ticketing service (Release 3, Phase 2; D55, D61, D63, D67): turns a feedback into a ticket, or into evidence on an open one.

`new()` is the one entry point (the Phase 3 pipeline calls it in-process; Admins reach it through the API and the "Create ticket" button):

1. read the feedback and build its masked evidence item (the question, the answer, the cited documents, the comment, the replay result);
2. look for a ticket from the same person, in the same knowledge base, with a cited document in common. A partial overlap is put to the
   ticketing agent ("same issue?"); the code makes the decision and uses the agent's answer only as a yes or no;
3. a matching OPEN ticket gets the item as evidence (its status and assignment stay); otherwise a new ticket is made, linked to an
   earlier CLOSED match as its related ticket;
4. a new ticket is assigned by the `ticket_assignment` rule.

The call is idempotent per feedback id: the same feedback never adds evidence twice or makes two tickets. There is no reopening: a
closed ticket stays closed. The model is never called inside a database transaction.
"""
import logging
import re
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import global_settings, guardrails, scoped_settings, tickets
from app.db import AnswerTrace, Database, Document, Feedback, KnowledgeBase, Message, Ticket, get_db
from app.tickets import Actor, NotFound, TicketError

log = logging.getLogger(__name__)
NEW_OUTCOME, EVIDENCE_OUTCOME = "new", "evidence_added"
MAX_AGENT_CANDIDATES = 5
TYPE_FEEDBACK = "feedback"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


# ---- evidence ----------------------------------------------------------------------------------------------------------------------
def mask(db: Database, text: str) -> str:
    """Evidence is masked again before it is stored (a second line of defence), always: tickets are read by more people and kept longer
    than conversations (D61). Fails closed."""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        masked, _ = guardrails.mask_with(text, guardrails.current(db))
        return masked
    except Exception as e:
        raise RuntimeError("the evidence could not be masked") from e


def build_item(db: Database, fb: Feedback, replay: dict | None = None) -> dict:
    """The evidence for one feedback: masked text, and the cited documents as ids and titles (never passage text)."""
    with db.session() as s:
        answer = s.get(Message, fb.answer_id)
        trace = s.get(AnswerTrace, fb.answer_id)
        question = s.get(Message, trace.question_id) if trace is not None else None
        doc_ids = []
        for hit in (trace.hits if trace is not None else []) or []:
            if hit.get("used") and hit.get("doc_id") is not None and hit["doc_id"] not in doc_ids:
                doc_ids.append(hit["doc_id"])
        titles = {d.id: d.name for d in s.scalars(select(Document).where(Document.id.in_(doc_ids)))} if doc_ids else {}
        raw = {"question": question.content if question else "", "answer": answer.content if answer else "", "comment": fb.comment or ""}
        links = list(fb.attachments or [])
    return {"feedback_id": fb.id, "question": mask(db, raw["question"]), "answer": mask(db, raw["answer"]), "comment": mask(db, raw["comment"]),
            "summary": mask(db, fb.summary or ""), "attachments": links, "documents": [{"doc_id": i, "title": titles.get(i, "")} for i in doc_ids], "replay": replay or None,
            "added_at": _now().isoformat()}


def _doc_ids(item: dict) -> set[int]:
    return {d["doc_id"] for d in item.get("documents") or [] if d.get("doc_id") is not None}


def _ticket_docs(t: Ticket) -> set[int]:
    out: set[int] = set()
    for item in t.evidence or []:
        out |= _doc_ids(item)
    return out


# ---- matching (D55) ---------------------------------------------------------------------------------------------------------------
def candidates(db: Database, fb: Feedback, item: dict) -> tuple[list[dict], list[dict]]:
    """(full, partial): earlier feedback tickets from the same person in the same knowledge base that share a cited document. `full` holds
    every document of this feedback; `partial` only some. Each entry: {id, status, updated_at, closed_at, title, overlap}. Most recently
    updated first."""
    mine = _doc_ids(item)
    if fb.user_id is None or fb.kb_id is None or not mine:
        return [], []
    full, partial = [], []
    with db.session() as s:
        kind = tickets.TicketType
        rows = s.scalars(select(Ticket).join(kind, kind.id == Ticket.type_id).where(
            kind.name == TYPE_FEEDBACK, Ticket.raised_by == fb.user_id, Ticket.kb_id == fb.kb_id).order_by(Ticket.updated_at.desc(), Ticket.id.desc()))
        for t in rows:
            shared = mine & _ticket_docs(t)
            if not shared:
                continue
            entry = {"id": t.id, "status": t.status, "updated_at": t.updated_at, "closed_at": t.closed_at, "title": t.title, "overlap": sorted(shared)}
            (full if shared == mine else partial).append(entry)
    return full, partial


def _agent_same_issue(item: dict, partial: list[dict]) -> set[int]:
    """The ticketing agent's yes or no for a partial overlap, as a set of ticket ids. If it fails the rule alone decides: no match."""
    if not partial:
        return set()
    try:
        from app import ticket_agent
        offered = partial[:MAX_AGENT_CANDIDATES]
        return {i for i in (ticket_agent.same_issue(item, offered) or ()) if i in {c["id"] for c in offered}}     # only ids that were offered count
    except Exception:
        log.warning("the ticketing agent could not compare tickets; only full overlaps match", exc_info=True)
        return set()


def choose(full: list[dict], partial: list[dict], agent_yes: set[int]) -> dict | None:
    """The match: the most recently updated of the full overlaps and the partial ones the agent said are the same issue."""
    pool = full + [p for p in partial if p["id"] in agent_yes]
    return max(pool, key=lambda e: (_aware(e["updated_at"]), e["id"])) if pool else None


# ---- the service ---------------------------------------------------------------------------------------------------------------------
def new(db: Database | None, feedback_id: int, *, type_name: str = TYPE_FEEDBACK, replay: dict | None = None) -> dict:
    """-> {outcome: new | evidence_added, ticket_id, existing}. `existing` is True when this feedback was already ticketed."""
    db = db or get_db()
    if type_name != TYPE_FEEDBACK:
        raise TicketError("Only feedback tickets can be made for now.")
    with db.session() as s:
        fb = s.get(Feedback, feedback_id)
        if fb is None:
            raise NotFound("No such feedback.")
        if fb.ticket_id:
            return _existing(s, fb)
        s.expunge(fb)
    item = build_item(db, fb, replay)
    full, partial = candidates(db, fb, item)
    match = choose(full, partial, _agent_same_issue(item, partial) if not full else set())    # the agent is asked only when no full overlap exists
    title = _title(db, item)
    out = _write(db, feedback_id, item, match, title)
    if not out["existing"]:
        if out["outcome"] == NEW_OUTCOME:
            assign(db, out["ticket_id"])
        else:
            _alert("evidence_added", out["ticket_id"])
    return out


def _alert(event: str, ticket_id: int) -> None:
    from app import ticket_alerts
    ticket_alerts.notify(None, event, ticket_id)                       # never raises, and never blocks the ticket


def _existing(s, fb: Feedback) -> dict:
    t = s.get(Ticket, fb.ticket_id)
    return {"outcome": NEW_OUTCOME if t is not None and t.ref_id == fb.id else EVIDENCE_OUTCOME, "ticket_id": fb.ticket_id, "existing": True}


def _title(db: Database, item: dict) -> str:
    """The masked ticket title: the ticketing agent's, or else the masked question."""
    try:
        from app import ticket_agent
        title = ticket_agent.title(item)
        if title:
            return mask(db, title)[:200]
    except Exception:
        log.warning("the ticketing agent could not write a title; the question is used", exc_info=True)
    first = (item.get("question") or item.get("comment") or "Feedback").strip().splitlines()[0]
    return first[:120]


def _related_window_ok(closed_at: datetime | None) -> bool:
    days = global_settings.current().ticket_related_window_days
    return days == 0 or (closed_at is not None and _aware(closed_at) >= _now() - timedelta(days=days))


_write_locks = [threading.Lock() for _ in range(64)]


def _write(db: Database, feedback_id: int, item: dict, match: dict | None, title: str) -> dict:
    """One ticket per feedback. On PostgreSQL the row lock (FOR UPDATE) makes two runners take turns, in any process. SQLite ignores that
    lock, so the calls of one process also take turns on a lock chosen by the feedback id."""
    with _write_locks[feedback_id % len(_write_locks)]:
        return _write_locked(db, feedback_id, item, match, title)


def _write_locked(db: Database, feedback_id: int, item: dict, match: dict | None, title: str) -> dict:
    with db.session() as s:
        fb = s.scalars(select(Feedback).where(Feedback.id == feedback_id).with_for_update()).first()
        if fb.ticket_id:                                              # another call got here first
            return _existing(s, fb)
        now = _now()
        target = s.get(Ticket, match["id"]) if match else None
        if target is not None and target.status in tickets.OPEN:
            if not any(i.get("feedback_id") == feedback_id for i in target.evidence or []):
                target.evidence = list(target.evidence or []) + [item]
            target.updated_at = now
            tickets._event(s, target.id, tickets.EVIDENCE_ADDED, Actor.system(), f"Feedback {feedback_id}",
                           {"feedback_id": feedback_id, "documents": [d["doc_id"] for d in item["documents"]]}, now)
            fb.ticket_id, fb.state, fb.updated_at = target.id, "ticketed", now
            return {"outcome": EVIDENCE_OUTCOME, "ticket_id": target.id, "existing": False}
        related = target.id if (target is not None and _related_window_ok(target.closed_at)) else None
        kb = s.get(KnowledgeBase, fb.kb_id) if fb.kb_id else None
        t = tickets.create(s, type_name=TYPE_FEEDBACK, ref_id=feedback_id, kb_id=fb.kb_id, department_id=kb.department_id if kb else fb.department_id,
                           title=title, evidence=[item], raised_by=fb.user_id, related_ticket_id=related)
        fb.ticket_id, fb.state, fb.updated_at = t.id, "ticketed", now
        return {"outcome": NEW_OUTCOME, "ticket_id": t.id, "existing": False}


# ---- assignment (D63) ----------------------------------------------------------------------------------------------------------------
def rule_for(t: Ticket) -> str:
    return scoped_settings.explain("ticket_assignment", kb_id=t.kb_id, department_id=t.department_id)[0]


def assignee_for(rule: str, agent_live: bool) -> tuple[str, str, int | None]:
    """(target status, assignee type, assignee id) for an assignment rule. `autonomous` is honoured only while the agent is live (D63)."""
    if rule == "autonomous" and agent_live:
        return tickets.ASSIGNED, tickets.AUTONOMOUS, None
    m = re.fullmatch(r"(user|group):(\d+)", rule or "")
    if m:
        return tickets.ASSIGNED_TO_HUMAN, m.group(1), int(m.group(2))
    return tickets.ASSIGNED_TO_HUMAN, "", None                        # the department's queue: its Editors and Admins see it and take it


def assign(db: Database | None, ticket_id: int) -> dict | None:
    """Assign a `new` ticket by its rule. Safe to run twice (a ticket that is no longer new is left alone)."""
    db = db or get_db()
    with db.session() as s:
        t = s.get(Ticket, ticket_id)
        if t is None or t.status != tickets.NEW:
            return None
        rule = rule_for(t)
    status, kind, who = assignee_for(rule, global_settings.current(db).admin_agent_mode == "live")
    try:
        out = tickets.transition(db, ticket_id, status, Actor.system(), assignee=(kind, who), expect=tickets.NEW, detail={"rule": rule})
    except tickets.IllegalTransition:
        return None
    if out["status"] == tickets.ASSIGNED_TO_HUMAN:
        _alert("assigned", ticket_id)
    return out


def assign_new(db: Database | None = None) -> int:
    """Assign every ticket still `new` (a creation that stopped before its assignment). Returns how many were assigned."""
    db = db or get_db()
    with db.session() as s:
        ids = list(s.scalars(select(Ticket.id).where(Ticket.status == tickets.NEW)))
    return sum(1 for i in ids if assign(db, i))


def release_queue(db: Database | None = None, reason: str = "The agent is off") -> list[int]:
    """Every ticket still `assigned` to the agent's queue goes to a person or the department by its rule, one event each (D63).
    Returns the ticket ids."""
    db = db or get_db()
    with db.session() as s:
        waiting = [(t.id, rule_for(t)) for t in s.scalars(select(Ticket).where(Ticket.status == tickets.ASSIGNED))]
    moved = []
    for tid, rule in waiting:
        _, kind, who = assignee_for(rule, False)
        try:
            tickets.transition(db, tid, tickets.ASSIGNED_TO_HUMAN, Actor.system(), assignee=(kind, who), expect=tickets.ASSIGNED, note=reason,
                               detail={"rule": rule, "released": True})
            moved.append(tid)
        except tickets.IllegalTransition:
            continue                                                  # the agent claimed it in the meantime: the lease handles it
    from app import ticket_alerts
    ticket_alerts.queue_released(db, moved)                           # one digest, not one alert per ticket
    return moved


# ---- retention (D49) ------------------------------------------------------------------------------------------------------------------
def retire(db: Database | None = None, now: datetime | None = None) -> int:
    """Delete closed tickets, with their events, `ticket_retention_days` after they were closed (0 keeps them for ever). Open tickets are
    never deleted. A ticket that pointed at a retired one loses only that link. Returns how many were deleted."""
    from sqlalchemy import delete, update
    from app.db import TicketEvent
    db = db or get_db()
    days = global_settings.current(db).ticket_retention_days
    if days <= 0:
        return 0
    cutoff = (now or _now()) - timedelta(days=days)
    with db.session() as s:
        ids = list(s.scalars(select(Ticket.id).where(Ticket.status.in_(tickets.CLOSED), Ticket.closed_at.is_not(None), Ticket.closed_at < cutoff)))
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            s.execute(update(Ticket).where(Ticket.related_ticket_id.in_(chunk)).values(related_ticket_id=None))
            s.execute(delete(TicketEvent).where(TicketEvent.ticket_id.in_(chunk)))
            s.execute(delete(Ticket).where(Ticket.id.in_(chunk)))
    return len(ids)
