"""Admin API for browsing and deleting stored conversations (Admin only; the text is the masked text).
Opening a conversation and deleting one are both audited (the audit entry never contains message text)."""
import csv
import hashlib
import hmac
import io
import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from app import global_settings, memory, retention
from app.config import settings
from app.admin.api import AdminContext, Session, admin_session, get_ctx
from app.db import Conversation, Message

router = APIRouter(prefix="/admin/api", tags=["admin"])
log = logging.getLogger(__name__)


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


# ---- retention ------------------------------------------------------------------------------------
@router.get("/conversations/retention/status")
def retention_status(_: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    runs = [e for e in ctx.store.recent_audit(500) if e["action"] == "retention.run"]
    last = runs[0] if runs else None
    return {"enabled": global_settings.current(ctx.db).retention_job_enabled, "preview": retention.plan(ctx.db),
            "last_run": ({"ts": last["ts"], "actor": last["actor"], "detail": last["detail"]} if last else None)}


@router.post("/conversations/retention/run")
def retention_run(sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    return retention.run(ctx.db, actor=sess.email)


# ---- export (D7): off until the privacy sign-off ----------------------------------------------------
class ExportBody(BaseModel):
    reason: str
    format: str = "csv"
    department: str = ""
    date_from: str = ""
    date_to: str = ""


def _pseudonym(email: str) -> str:
    """A stable reference for a person that does not reveal their email."""
    if not email:
        return "unknown"
    return hmac.new((settings.secret_key or "caa").encode(), email.lower().encode(), hashlib.sha256).hexdigest()[:12]


COLUMNS = ["time", "conversation", "person", "department", "channel", "role", "complete", "text"]


@router.post("/conversations/export")
def export(body: ExportBody, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    if not global_settings.current(ctx.db).export_enabled:
        raise HTTPException(403, "Export is turned off. An Admin can turn it on in Settings once the privacy sign-off is recorded.")
    reason = (body.reason or "").strip()
    if len(reason) < 10:
        raise HTTPException(422, "Give a reason for the export (at least 10 characters). It is recorded in the audit log.")
    if body.format not in ("csv", "json"):
        raise HTTPException(422, "Format must be csv or json.")
    start, end = _day(body.date_from), _day(body.date_to, True)
    q = select(Message, Conversation).join(Conversation, Conversation.id == Message.conversation_id).order_by(Message.id)
    if body.department:
        q = q.where(Conversation.department == body.department)
    if start:
        q = q.where(Message.ts >= start)
    if end:
        q = q.where(Message.ts < end)
    ctx.store.record(sess.email, "conversation.export", body.format,
                     f"reason: {reason[:300]}; department: {body.department or 'all'}; from {body.date_from or 'start'} to {body.date_to or 'now'}")

    def rows():
        last = 0
        while True:
            with ctx.db.session() as s:
                batch = [(m.id, m.ts, m.conversation_id, m.role, m.complete, m.content, c.owner_email, c.department, c.channel)
                         for m, c in s.execute(q.where(Message.id > last).limit(500)).all()]
            if not batch:
                return
            last = batch[-1][0]
            for r in batch:
                yield {"time": r[1].isoformat(), "conversation": hashlib.sha256(r[2].encode()).hexdigest()[:12], "person": _pseudonym(r[6]),
                       "department": r[7], "channel": r[8], "role": r[3], "complete": r[4], "text": r[5]}

    def stream():
        count = 0
        try:
            if body.format == "json":
                yield "["
                for i, row in enumerate(rows()):
                    yield ("," if i else "") + json.dumps(row, ensure_ascii=False)
                    count += 1
                yield "]"
            else:
                buf = io.StringIO(); w = csv.DictWriter(buf, fieldnames=COLUMNS); w.writeheader()
                yield buf.getvalue()
                for row in rows():
                    buf = io.StringIO(); csv.DictWriter(buf, fieldnames=COLUMNS).writerow(row)
                    yield buf.getvalue()
                    count += 1
        finally:
            ctx.store.record(sess.email, "conversation.export.done", body.format, f"{count} messages")

    name = f"conversations-{datetime.now(timezone.utc):%Y%m%d-%H%M}.{body.format}"
    return StreamingResponse(stream(), media_type="text/csv" if body.format == "csv" else "application/json",
                             headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


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
