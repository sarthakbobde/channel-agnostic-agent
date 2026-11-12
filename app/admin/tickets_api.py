"""Admin API for tickets (Release 3, Phase 2; D55, D56, A8). An Admin works every ticket, an Editor the tickets of the departments they
edit, and a person (or group member) a ticket assigned to them. Every change goes through app/tickets.py, which enforces the allowed
moves and who may make them. Changes are audited with the ticket number and the new status, never with any text.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from app import admin_fixes, global_settings, identity, ticketing, tickets
from app.admin.api import AdminContext, Session, admin_session, get_ctx
from app.admin.feedback_api import _scope as feedback_scope
from app.db import Feedback, KnowledgeBase, Ticket, TicketEvent, TicketType

router = APIRouter(prefix="/admin/api/tickets", tags=["admin"])


def _actor(sess: Session, ctx: AdminContext) -> tickets.Actor:
    return tickets.Actor.person(identity.resolve(sess.email, ctx.db))


def _fail(e: tickets.TicketError):
    raise HTTPException(e.status, str(e))


def _audit(ctx: AdminContext, sess: Session, action: str, ticket_id: int, detail: str = "") -> None:
    ctx.store.record(sess.email, f"ticket.{action}", f"ticket {ticket_id}", detail)


class Note(BaseModel):
    note: str = ""


class Reassign(BaseModel):
    to_type: str
    to_id: int
    note: str = ""


class NewTicket(BaseModel):
    feedback_id: int


@router.get("")
def list_tickets(status: str = "", kb_id: int = 0, mine: bool = False, open_only: bool = False, limit: int = 25, offset: int = 0,
                 sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    limit, offset = max(1, min(limit, 200)), max(0, offset)
    actor = _actor(sess, ctx)
    q = tickets.view_scope(select(Ticket), actor)
    if status:
        if status not in tickets.STATUSES:
            raise HTTPException(422, "Unknown status.")
        q = q.where(Ticket.status == status)
    if open_only:
        q = q.where(Ticket.status.in_(tickets.OPEN))
    if kb_id:
        q = q.where(Ticket.kb_id == kb_id)
    if mine and actor.user_id is not None:
        q = q.where(Ticket.assignee_type == tickets.USER, Ticket.assignee_id == actor.user_id)
    with ctx.db.session() as s:
        total = s.scalar(select(func.count()).select_from(q.subquery())) or 0
        rows = list(s.scalars(q.order_by(Ticket.updated_at.desc(), Ticket.id.desc()).limit(limit).offset(offset)))
        kbs = dict(s.execute(select(KnowledgeBase.id, KnowledgeBase.name)).all())
        return {"total": total, "statuses": list(tickets.STATUSES),
                "items": [{**tickets.as_dict(t), "kb": kbs.get(t.kb_id, "") if t.kb_id else ""} for t in rows]}


@router.get("/report")
def report(sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """Counts and times only; no text. Open tickets by status and age, time to close, evidence added, tickets that point at a closed one."""
    actor = _actor(sess, ctx)
    base = tickets.view_scope(select(Ticket), actor).subquery()
    now = datetime.now(timezone.utc)
    with ctx.db.session() as s:
        by_status = [{"name": n, "count": c} for n, c in s.execute(select(base.c.status, func.count()).group_by(base.c.status)).all()]
        total = sum(d["count"] for d in by_status)
        open_rows = s.execute(select(base.c.created_at).where(base.c.status.in_(tickets.OPEN))).all()
        ages = {"under_1_day": 0, "1_to_7_days": 0, "over_7_days": 0}
        for (created,) in open_rows:
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            days = (now - created).total_seconds() / 86400
            ages["under_1_day" if days < 1 else "1_to_7_days" if days <= 7 else "over_7_days"] += 1
        closed = s.execute(select(base.c.created_at, base.c.closed_at).where(base.c.closed_at.is_not(None))).all()
        hours = []
        for created, closed_at in closed:
            c, d = (created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created), (closed_at.replace(tzinfo=timezone.utc) if closed_at.tzinfo is None else closed_at)
            hours.append((d - c).total_seconds() / 3600)
        evidence = s.scalar(select(func.count()).select_from(TicketEvent).where(TicketEvent.event == tickets.EVIDENCE_ADDED,
                                                                                 TicketEvent.ticket_id.in_(select(base.c.id)))) or 0
        related = s.scalar(select(func.count()).select_from(base).where(base.c.related_ticket_id.is_not(None))) or 0
        return {"total": total, "by_status": by_status, "open_by_age": ages, "closed": len(hours),
                "average_hours_to_close": round(sum(hours) / len(hours), 1) if hours else None,
                "evidence_additions": evidence, "related_tickets": related,
                "related_share": round(related / total, 3) if total else None}


@router.post("/new")
def new_ticket(body: NewTicket, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """Raise a ticket from a negative feedback by hand (an override of the pipeline). The same service as the automatic handover, so the
    same rules apply: one ticket per feedback, evidence added to an open ticket on the same problem. Audited."""
    with ctx.db.session() as s:
        fb = s.scalar(feedback_scope(select(Feedback).where(Feedback.id == body.feedback_id), ctx, sess))
        if fb is None:
            raise HTTPException(404, "No such feedback.")
        if fb.rating != "down":
            raise HTTPException(422, "Only a negative feedback can become a ticket.")
    try:
        out = ticketing.new(ctx.db, body.feedback_id)
    except tickets.TicketError as e:
        _fail(e)
    _audit(ctx, sess, "create", out["ticket_id"], f"from feedback {body.feedback_id}: {out['outcome']}")
    return out


@router.get("/agent-report")
def agent_report(sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """What the admin agent did: tickets handled, fixes by type, handovers with their reasons, rollbacks, shadow diagnoses, proposals. Admin only; counts only."""
    if identity.resolve(sess.email, ctx.db).role != "admin":
        raise HTTPException(403, "Only an Admin can see the agent report.")
    with ctx.db.session() as s:
        events = list(s.scalars(select(TicketEvent).where(TicketEvent.event.in_((tickets.COMPLETED, tickets.DISMISSED, tickets.ASSIGNED_TO_HUMAN, tickets.FIX_APPLIED,
                                                                                tickets.DIAGNOSIS, tickets.PROPOSAL, tickets.PROPOSAL_DECIDED)))))
    handled = {"completed": 0, "dismissed": 0, "handed_over": 0}
    fixes: dict = {}
    reasons: dict = {}
    rollbacks = shadow = live = proposals = approved = declined = 0
    for e in events:
        d = e.detail or {}
        if e.event == tickets.FIX_APPLIED:
            row = fixes.setdefault(d.get("action") or "?", {"attempted": 0, "succeeded": 0, "rolled_back": 0})
            row["attempted"] += 1
            row["succeeded"] += 1 if d.get("ok") else 0
            row["rolled_back"] += 1 if d.get("rolled_back") else 0
            rollbacks += 1 if d.get("rolled_back") else 0
        elif e.event == tickets.DIAGNOSIS:
            shadow, live = shadow + (1 if d.get("shadow") else 0), live + (0 if d.get("shadow") else 1)
        elif e.event == tickets.PROPOSAL:
            proposals += 1
        elif e.event == tickets.PROPOSAL_DECIDED:
            approved, declined = approved + (1 if d.get("decision") == "approved" else 0), declined + (1 if d.get("decision") == "declined" else 0)
        elif e.actor_type in ("autonomous", "system") and (d.get("reason") or e.event != tickets.ASSIGNED_TO_HUMAN or e.actor_type == "autonomous"):
            if e.event == tickets.ASSIGNED_TO_HUMAN:
                if e.actor_type == "autonomous" or d.get("reason") == "timeout":
                    handled["handed_over"] += 1
                    reasons[d.get("reason") or "unknown"] = reasons.get(d.get("reason") or "unknown", 0) + 1
            elif e.actor_type == "autonomous":
                handled[e.event] += 1
    return {"handled": handled, "fixes": fixes, "handover_reasons": reasons, "rollbacks": rollbacks, "diagnoses": {"live": live, "shadow": shadow},
            "proposals": {"made": proposals, "approved": approved, "declined": declined}, "mode": global_settings.current(ctx.db).admin_agent_mode}


@router.get("/{ticket_id}")
def get_ticket(ticket_id: int, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    actor = _actor(sess, ctx)
    with ctx.db.session() as s:
        t = s.get(Ticket, ticket_id)
        if t is None or not tickets.can_see(actor, t):
            raise HTTPException(404, "No such ticket.")
        kind = s.get(TicketType, t.type_id)
        kb = s.get(KnowledgeBase, t.kb_id) if t.kb_id else None
        out = {**tickets.as_dict(t, kind.name if kind else ""), "kb": kb.name if kb else "", "evidence": t.evidence or []}
    out["events"] = tickets.events(ctx.db, ticket_id)
    out["can_manage"] = tickets.is_manager(actor, t)
    out["can_work"] = tickets.is_worker(actor, t)
    out["proposal"] = admin_fixes.proposal_event(ctx.db, ticket_id) if out["can_manage"] else None
    return out


def _move(ticket_id: int, to: str, action: str, body: Note, sess: Session, ctx: AdminContext, need_note: bool = False) -> dict:
    note = (body.note or "").strip()
    if need_note and len(note) < 3:
        raise HTTPException(422, "Write a short note first. It is kept with the ticket.")
    actor = _actor(sess, ctx)
    with ctx.db.session() as s:
        t = s.get(Ticket, ticket_id)
        if t is None or not tickets.can_see(actor, t):
            raise HTTPException(404, "No such ticket.")
    try:
        out = tickets.transition(ctx.db, ticket_id, to, actor, note=note)
    except tickets.TicketError as e:
        _fail(e)
    _audit(ctx, sess, action, ticket_id, f"now {out['status']}")
    return out


@router.post("/{ticket_id}/take")
def take(ticket_id: int, body: Note = Note(), sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    return _move(ticket_id, tickets.IN_PROGRESS, "take", body, sess, ctx)


@router.post("/{ticket_id}/hold")
def hold(ticket_id: int, body: Note, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    return _move(ticket_id, tickets.HOLD, "hold", body, sess, ctx, need_note=True)


@router.post("/{ticket_id}/complete")
def complete(ticket_id: int, body: Note, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    return _move(ticket_id, tickets.COMPLETED, "complete", body, sess, ctx, need_note=True)


@router.post("/{ticket_id}/dismiss")
def dismiss(ticket_id: int, body: Note, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    return _move(ticket_id, tickets.DISMISSED, "dismiss", body, sess, ctx, need_note=True)


@router.post("/{ticket_id}/reassign")
def reassign(ticket_id: int, body: Reassign, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    actor = _actor(sess, ctx)
    with ctx.db.session() as s:
        t = s.get(Ticket, ticket_id)
        if t is None or not tickets.can_see(actor, t):
            raise HTTPException(404, "No such ticket.")
    try:
        out = tickets.reassign(ctx.db, ticket_id, body.to_type, body.to_id, actor, (body.note or "").strip())
    except tickets.TicketError as e:
        _fail(e)
    _audit(ctx, sess, "reassign", ticket_id, f"to {body.to_type} {body.to_id}")
    ticketing._alert("assigned", ticket_id)
    return out


@router.post("/{ticket_id}/comment")
def add_comment(ticket_id: int, body: Note, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    try:
        out = tickets.comment(ctx.db, ticket_id, _actor(sess, ctx), body.note)
    except tickets.TicketError as e:
        _fail(e)
    return out


def _decide(ticket_id: int, body: Note, approve: bool, sess: Session, ctx: AdminContext) -> dict:
    actor = _actor(sess, ctx)
    with ctx.db.session() as s:
        t = s.get(Ticket, ticket_id)
        if t is None or not tickets.can_see(actor, t):
            raise HTTPException(404, "No such ticket.")
    try:
        out = admin_fixes.approve(ctx.db, ticket_id, actor, sess.email, approve=approve, note=body.note)
    except tickets.TicketError as e:
        _fail(e)
    _audit(ctx, sess, "approve" if approve else "decline", ticket_id, f"{out['decision']}; ok={out['ok']}")
    return out


@router.post("/{ticket_id}/proposal/approve")
def approve_proposal(ticket_id: int, body: Note = Note(), sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    return _decide(ticket_id, body, True, sess, ctx)


@router.post("/{ticket_id}/proposal/decline")
def decline_proposal(ticket_id: int, body: Note = Note(), sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    return _decide(ticket_id, body, False, sess, ctx)
