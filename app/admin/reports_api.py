"""Admin reports: guardrails, knowledge bases and channels. Read-only summaries of what is already stored; Admin only."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select

from app.admin.api import AdminContext, Session, admin_session, get_ctx
from app.db import Channel, Conversation, Document, GuardrailEvent, KnowledgeBase, Message, UsageRecord

router = APIRouter(prefix="/admin/api/reports", tags=["admin"])


def _day(text: str, end: bool = False):
    if not text:
        return None
    try:
        d = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(422, "Dates look like 2026-10-31.")
    return d + timedelta(days=1) if end else d


@router.get("/guardrails")
def guardrail_report(date_from: str = "", date_to: str = "", _: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """How often each check fired, by direction, action and department."""
    lo, hi = _day(date_from), _day(date_to, True)
    out = {}
    with ctx.db.session() as s:
        for key, col in (("check", GuardrailEvent.check), ("direction", GuardrailEvent.direction),
                         ("action", GuardrailEvent.action), ("department", GuardrailEvent.department)):
            q = select(col, func.count()).group_by(col).order_by(func.count().desc())
            if lo:
                q = q.where(GuardrailEvent.ts >= lo)
            if hi:
                q = q.where(GuardrailEvent.ts < hi)
            out["by_" + key] = [{"name": n or "(none)", "count": c} for n, c in s.execute(q).all()]
    out["total"] = sum(r["count"] for r in out["by_check"])
    return out


@router.get("/knowledge")
def knowledge_report(date_from: str = "", date_to: str = "", _: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """Per knowledge base: documents by status, chunks, size, last indexed, answered vs not-found, embedding tokens."""
    lo, hi = _day(date_from), _day(date_to, True)
    with ctx.db.session() as s:
        kbs = s.scalars(select(KnowledgeBase).order_by(KnowledgeBase.name)).all()
        docs = {}
        for kb_id, status, n, chunks, size, last in s.execute(
                select(Document.kb_id, Document.status, func.count(), func.coalesce(func.sum(Document.chunks), 0),
                       func.coalesce(func.sum(Document.size), 0), func.max(Document.indexed_at)).group_by(Document.kb_id, Document.status)).all():
            d = docs.setdefault(kb_id, {"by_status": {}, "chunks": 0, "size_bytes": 0, "last_indexed": None})
            d["by_status"][status] = n
            d["chunks"] += int(chunks)
            d["size_bytes"] += int(size)
            if last and (d["last_indexed"] is None or last > d["last_indexed"]):
                d["last_indexed"] = last
        answered, notfound = {}, {}
        q = select(Message.meta).where(Message.role == "assistant")
        if lo:
            q = q.where(Message.ts >= lo)
        if hi:
            q = q.where(Message.ts < hi)
        for (meta,) in s.execute(q).all():
            if not isinstance(meta, dict) or not meta.get("searched"):
                continue
            bucket = answered if meta.get("found", bool(meta.get("sources"))) else notfound
            for kid in meta.get("kb_ids") or []:
                bucket[kid] = bucket.get(kid, 0) + 1
        tq = select(UsageRecord.kb_id, func.coalesce(func.sum(UsageRecord.input_tokens), 0)).where(UsageRecord.kind == "embedding").group_by(UsageRecord.kb_id)
        if lo:
            tq = tq.where(UsageRecord.ts >= lo)
        if hi:
            tq = tq.where(UsageRecord.ts < hi)
        tokens = {k: int(t) for k, t in s.execute(tq).all()}
        rows = []
        for kb in kbs:
            d = docs.get(kb.id, {"by_status": {}, "chunks": 0, "size_bytes": 0, "last_indexed": None})
            rows.append({"id": kb.id, "name": kb.name, **d, "answered": answered.get(kb.id, 0), "not_found": notfound.get(kb.id, 0),
                         "embedding_tokens": tokens.get(kb.id, 0)})
    return {"rows": rows}


@router.get("/channels")
def channel_report(_: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """Per channel: switched on, running, conversations, last activity."""
    with ctx.db.session() as s:
        convs = {c: (n, last) for c, n, last in s.execute(
            select(Conversation.channel, func.count(), func.max(Conversation.updated_at)).group_by(Conversation.channel)).all()}
        enabled = {c.type: c.enabled for c in s.scalars(select(Channel)).all()}
    rows = []
    for typ in ctx.manager.registry:
        n, last = convs.get(typ, (0, None))
        adapter = ctx.manager.get(typ)
        rows.append({"type": typ, "title": ctx.manager.adapter_class(typ).title, "enabled": bool(enabled.get(typ, False)),
                     "running": adapter is not None, "conversations": n, "last_activity": last})
    return {"rows": rows}


@router.get("/suggestions")
def suggestion_report(date_from: str = "", date_to: str = "", _: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """Suggested follow-ups, from the stored rows: shown, clicked and click-through by channel and knowledge base, why
    candidates were dropped, and how often the model's reply had no usable suggestion block."""
    from app.db import Suggestion
    lo, hi = _day(date_from), _day(date_to, True)

    def window(q, col):
        if lo:
            q = q.where(col >= lo)
        if hi:
            q = q.where(col < hi)
        return q
    with ctx.db.session() as s:
        names = dict(s.execute(select(KnowledgeBase.id, KnowledgeBase.name)).all())
        rows = s.execute(window(select(Suggestion.channel, Suggestion.kb_id, func.count(), func.count(Suggestion.clicked_at))
                                .where(Suggestion.outcome == "Shown").group_by(Suggestion.channel, Suggestion.kb_id), Suggestion.created_at)).all()
        outcomes = s.execute(window(select(Suggestion.outcome, func.count()).group_by(Suggestion.outcome), Suggestion.created_at)).all()
        no_block = s.scalar(window(select(func.coalesce(func.sum(GuardrailEvent.count), 0))
                                   .where(GuardrailEvent.check == "suggestions", GuardrailEvent.action == "no_block"), GuardrailEvent.ts))
    return {"rows": [{"channel": ch, "kb": names.get(kb, "–"), "shown": shown, "clicked": clicked,
                      "click_rate": round(clicked / shown, 3) if shown else 0.0} for ch, kb, shown, clicked in rows],
            "outcomes": [{"name": o, "count": n} for o, n in outcomes], "no_block": int(no_block or 0)}
