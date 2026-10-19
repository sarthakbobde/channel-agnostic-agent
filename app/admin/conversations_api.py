"""Admin API for browsing and deleting stored conversations (Admin only; the text is the masked text).
Opening a conversation and deleting one are both audited (the audit entry never contains message text)."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select

from app import memory
from app.admin.api import AdminContext, Session, admin_session, get_ctx
from app.db import Conversation, Message

router = APIRouter(prefix="/admin/api", tags=["admin"])


def _day(text: str | None, end: bool = False):
    if not text:
        return None
    try:
        d = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(422, "Dates look like 2026-10-31.")
    return d + timedelta(days=1) if end else d


@router.get("/conversations")
def list_conversations(department: str = "", channel: str = "", person: str = "", date_from: str = "", date_to: str = "",
                       limit: int = 50, offset: int = 0, _: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    limit, offset = max(1, min(limit, 200)), max(0, offset)
    q = select(Conversation)
    if department:
        q = q.where(Conversation.department == department)
    if channel:
        q = q.where(Conversation.channel == channel)
    if person:
        q = q.where(func.lower(Conversation.owner_email).like("%" + person.lower().replace("%", "") + "%"))
    if _day(date_from):
        q = q.where(Conversation.updated_at >= _day(date_from))
    if _day(date_to, True):
        q = q.where(Conversation.updated_at < _day(date_to, True))
    with ctx.db.session() as s:
        total = s.scalar(select(func.count()).select_from(q.subquery())) or 0
        rows = list(s.scalars(q.order_by(Conversation.updated_at.desc()).limit(limit).offset(offset)))
        counts = dict(s.execute(select(Message.conversation_id, func.count()).where(Message.conversation_id.in_([c.id for c in rows]))
                                .group_by(Message.conversation_id)).all()) if rows else {}
        departments = [d for (d,) in s.execute(select(Conversation.department).distinct().order_by(Conversation.department)).all() if d]
        return {"total": total, "departments": departments,
                "conversations": [{"id": c.id, "channel": c.channel, "person": c.owner_email or "unknown", "department": c.department,
                                   "title": c.title or "New chat", "messages": counts.get(c.id, 0), "updated_at": c.updated_at,
                                   "created_at": c.created_at} for c in rows]}


@router.get("/conversations/{conv_id}")
def open_conversation(conv_id: str, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    with ctx.db.session() as s:
        c = s.get(Conversation, conv_id)
        if c is None:
            raise HTTPException(404, "No such conversation.")
        info = {"id": c.id, "channel": c.channel, "person": c.owner_email or "unknown", "department": c.department, "title": c.title}
    ctx.store.record(sess.email, "conversation.view", conv_id)
    return {**info, "messages": memory.get_messages(conv_id, ctx.db)}


@router.delete("/conversations/{conv_id}")
def delete_conversation(conv_id: str, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    if not memory.delete(conv_id, ctx.db):
        raise HTTPException(404, "No such conversation.")
    ctx.store.record(sess.email, "conversation.delete", conv_id)
    return {"ok": True}
