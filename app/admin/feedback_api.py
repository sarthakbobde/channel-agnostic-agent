"""Admin API for feedback on answers (Release 3, Phase 1; D49, A8). An Admin sees all feedback; an Editor sees the feedback on the
knowledge bases of the departments they edit. A Viewer has no panel. Comments are the masked text; the answer text itself is only
reached through the trace view, which has its own access rules. Reading is not audited (no text beyond the masked comment).
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select

from app import feedback, identity
from app.admin.api import AdminContext, Session, admin_session, get_ctx
from app.admin.conversations_api import _day
from app.db import Conversation, Feedback, KnowledgeBase

router = APIRouter(prefix="/admin/api/feedback", tags=["admin"])


def _scope(q, ctx: AdminContext, sess: Session):
    """Limit a query on Feedback to what this person may see: every row for an Admin, the rows on the knowledge bases of their own
    departments for an Editor (a row with no knowledge base is Admin only)."""
    who = identity.resolve(sess.email, ctx.db)
    if who.role == "admin":
        return q
    mine = set(who.edits_departments)
    return q.where(Feedback.kb_id.in_(select(KnowledgeBase.id).where(KnowledgeBase.department_id.in_(mine)))) if mine else q.where(Feedback.id < 0)


def _filtered(q, state, rating, channel, kb_id, reason, date_from, date_to):
    if state:
        if state not in feedback.STATES:
            raise HTTPException(422, "Unknown state.")
        q = q.where(Feedback.state == state)
    if rating:
        if rating not in (feedback.UP, feedback.DOWN):
            raise HTTPException(422, "Rating is up or down.")
        q = q.where(Feedback.rating == rating)
    if channel:
        q = q.where(Feedback.channel == channel)
    if kb_id:
        q = q.where(Feedback.kb_id == kb_id)
    if reason:
        q = q.where(Feedback.reason == reason)
    if _day(date_from):
        q = q.where(Feedback.created_at >= _day(date_from))
    if _day(date_to, True):
        q = q.where(Feedback.created_at < _day(date_to, True))
    return q


@router.get("")
def list_feedback(state: str = "", rating: str = "", channel: str = "", kb_id: int = 0, reason: str = "", date_from: str = "", date_to: str = "",
                  limit: int = 25, offset: int = 0, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    limit, offset = max(1, min(limit, 200)), max(0, offset)
    q = _filtered(_scope(select(Feedback), ctx, sess), state, rating, channel, kb_id, reason, date_from, date_to)
    with ctx.db.session() as s:
        total = s.scalar(select(func.count()).select_from(q.subquery())) or 0
        rows = list(s.scalars(q.order_by(Feedback.created_at.desc(), Feedback.id.desc()).limit(limit).offset(offset)))
        names = dict(s.execute(select(KnowledgeBase.id, KnowledgeBase.name)).all())
        people = dict(s.execute(select(Conversation.id, Conversation.owner_email).where(Conversation.id.in_([r.conversation_id for r in rows]))).all()) if rows else {}
        kbs = [{"id": i, "name": n} for i, n in sorted(names.items(), key=lambda x: x[1])]
        return {"total": total, "states": list(feedback.STATES), "reasons": list(feedback.REASONS), "knowledge_bases": kbs,
                "items": [{"id": r.id, "answer_id": r.answer_id, "conversation_id": r.conversation_id, "person": people.get(r.conversation_id) or "unknown",
                           "kb_id": r.kb_id, "kb": names.get(r.kb_id, "") if r.kb_id else "", "channel": r.channel, "rating": r.rating,
                           "reason": r.reason, "comment": r.comment, "attachments": r.attachments or [], "state": r.state,
                           "intent": r.intent, "created_at": r.created_at, "updated_at": r.updated_at} for r in rows]}


@router.get("/report")
def report(date_from: str = "", date_to: str = "", sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """Ratings by channel, knowledge base, reason and state. Counts only; no text."""
    base = _filtered(_scope(select(Feedback), ctx, sess), "", "", "", 0, "", date_from, date_to).subquery()
    with ctx.db.session() as s:
        def split(col):
            rows = s.execute(select(col, base.c.rating, func.count()).group_by(col, base.c.rating)).all()
            out: dict = {}
            for name, rating, n in rows:
                d = out.setdefault(name, {"name": name, "up": 0, "down": 0})
                d[rating] = n
            return sorted(({**d, "total": d["up"] + d["down"]} for d in out.values()), key=lambda d: -d["total"])
        names = dict(s.execute(select(KnowledgeBase.id, KnowledgeBase.name)).all())
        by_kb = split(base.c.kb_id)
        for d in by_kb:
            d["name"] = names.get(d["name"], "(none)") if d["name"] else "(none)"
        up = s.scalar(select(func.count()).select_from(base).where(base.c.rating == feedback.UP)) or 0
        down = s.scalar(select(func.count()).select_from(base).where(base.c.rating == feedback.DOWN)) or 0
        by_reason = [{"name": n or "(none)", "count": c} for n, c in s.execute(
            select(base.c.reason, func.count()).where(base.c.rating == feedback.DOWN).group_by(base.c.reason).order_by(func.count().desc())).all()]
        by_state = [{"name": n, "count": c} for n, c in s.execute(select(base.c.state, func.count()).group_by(base.c.state).order_by(func.count().desc())).all()]
        return {"total": up + down, "up": up, "down": down, "positive_share": round(up / (up + down), 3) if up + down else None,
                "by_channel": split(base.c.channel), "by_kb": by_kb, "by_reason": by_reason, "by_state": by_state}
