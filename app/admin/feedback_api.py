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


def _filtered(q, state, rating, channel, kb_id, date_from, date_to):
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
    if _day(date_from):
        q = q.where(Feedback.created_at >= _day(date_from))
    if _day(date_to, True):
        q = q.where(Feedback.created_at < _day(date_to, True))
    return q


@router.get("")
def list_feedback(state: str = "", rating: str = "", channel: str = "", kb_id: int = 0, date_from: str = "", date_to: str = "",
                  limit: int = 25, offset: int = 0, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    limit, offset = max(1, min(limit, 200)), max(0, offset)
    q = _filtered(_scope(select(Feedback), ctx, sess), state, rating, channel, kb_id, date_from, date_to)
    with ctx.db.session() as s:
        total = s.scalar(select(func.count()).select_from(q.subquery())) or 0
        rows = list(s.scalars(q.order_by(Feedback.created_at.desc(), Feedback.id.desc()).limit(limit).offset(offset)))
        names = dict(s.execute(select(KnowledgeBase.id, KnowledgeBase.name)).all())
        people = dict(s.execute(select(Conversation.id, Conversation.owner_email).where(Conversation.id.in_([r.conversation_id for r in rows]))).all()) if rows else {}
        kbs = [{"id": i, "name": n} for i, n in sorted(names.items(), key=lambda x: x[1])]
        return {"total": total, "states": list(feedback.STATES), "knowledge_bases": kbs,
                "items": [{"id": r.id, "answer_id": r.answer_id, "conversation_id": r.conversation_id, "person": people.get(r.conversation_id) or "unknown",
                           "kb_id": r.kb_id, "kb": names.get(r.kb_id, "") if r.kb_id else "", "channel": r.channel, "rating": r.rating,
                           "comment": r.comment, "attachments": r.attachments or [], "state": r.state,
                           "intent": r.intent, "ticket_id": r.ticket_id, "created_at": r.created_at, "updated_at": r.updated_at} for r in rows]}


@router.get("/report")
def report(date_from: str = "", date_to: str = "", sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """Ratings by channel, knowledge base and state. Counts only; no text."""
    base = _filtered(_scope(select(Feedback), ctx, sess), "", "", "", 0, date_from, date_to).subquery()
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
        by_state = [{"name": n, "count": c} for n, c in s.execute(select(base.c.state, func.count()).group_by(base.c.state).order_by(func.count().desc())).all()]
        return {"total": up + down, "up": up, "down": down, "positive_share": round(up / (up + down), 3) if up + down else None,
                "by_channel": split(base.c.channel), "by_kb": by_kb, "by_state": by_state}


# ---- human review and the processing report (Release 3, Phase 3; D54, A8) ---------------------------------------------------------------
from datetime import datetime, timezone  # noqa: E402

from pydantic import BaseModel  # noqa: E402

from app import feedback_flow, global_settings, pii, replay, ticketing  # noqa: E402
from app.db import AnswerTrace, Message, UsageRecord  # noqa: E402


class Claim(BaseModel):
    pass


class Decision(BaseModel):
    decision: str
    note: str = ""


def _text(ctx: AdminContext, value: str) -> str:
    return pii.label(ticketing.mask(ctx.db, value or "")) if value else ""


def _aware(d):
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


@router.get("/review")
def review_queue(limit: int = 25, offset: int = 0, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """Items waiting for a person: the question, the answer, the comment, the issue summary and the replay result side by side."""
    limit, offset = max(1, min(limit, 100)), max(0, offset)
    g = global_settings.current(ctx.db)
    q = _scope(select(Feedback).where(Feedback.state == feedback_flow.HUMAN_REVIEW), ctx, sess)
    now = datetime.now(timezone.utc)
    with ctx.db.session() as s:
        total = s.scalar(select(func.count()).select_from(q.subquery())) or 0
        rows = list(s.scalars(q.order_by(Feedback.created_at, Feedback.id).limit(limit).offset(offset)))
        names = dict(s.execute(select(KnowledgeBase.id, KnowledgeBase.name)).all())
        items = []
        for r in rows:
            t = s.get(AnswerTrace, r.answer_id)
            question = s.get(Message, t.question_id) if t is not None else None
            answer = s.get(Message, r.answer_id)
            v = r.verification if isinstance(r.verification, dict) else {}
            age = (now - _aware(r.created_at)).total_seconds() / 86400            # since it was rated: holding or touching an item must not reset it
            held = bool(r.claimed_by and _aware(r.claimed_until) and _aware(r.claimed_until) > now)
            items.append({"id": r.id, "kb": names.get(r.kb_id, "") if r.kb_id else "", "channel": r.channel, "rating": r.rating,
                          "question": _text(ctx, question.content if question else ""), "answer": _text(ctx, answer.content if answer else ""),
                          "comment": r.comment, "attachments": r.attachments or [], "summary": r.summary, "intent": r.intent, "intent_source": r.intent_source,
                          "reason": r.review_reason, "verification": ({"outcome": v.get("outcome"), "reason": v.get("reason"), "found": v.get("found"),
                                                                        "replay_answer": v.get("replay_answer", ""), "settings_changed": v.get("settings_changed") or [],
                                                                        "cached": bool(v.get("cached"))} if v else None),
                          "age_days": round(age, 1), "old": age >= g.feedback_review_age_days, "held_by": r.claimed_by if held else "", "created_at": r.created_at})
        return {"total": total, "age_days": g.feedback_review_age_days, "items": items}


def _reviewable(fid: int, ctx: AdminContext, sess: Session) -> int:
    with ctx.db.session() as s:
        if s.scalar(_scope(select(Feedback.id).where(Feedback.id == fid), ctx, sess)) is None:
            raise HTTPException(404, "No such feedback.")
    who = identity.resolve(sess.email, ctx.db)
    if who.user_id is None:
        raise HTTPException(403, "Only a signed-in person can review.")
    return who.user_id


@router.post("/review/{fid}/claim")
def review_claim(fid: int, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    uid = _reviewable(fid, ctx, sess)
    try:
        return feedback_flow.claim_review(ctx.db, fid, uid)
    except feedback_flow.ReviewError as e:
        raise HTTPException(e.status, str(e))


@router.post("/review/{fid}/decide")
def review_decide(fid: int, body: Decision, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    uid = _reviewable(fid, ctx, sess)
    try:
        out = feedback_flow.decide(ctx.db, fid, uid, body.decision, body.note)
    except feedback_flow.ReviewError as e:
        raise HTTPException(e.status, str(e))
    ctx.store.record(sess.email, "feedback.review", f"feedback {fid}", f"{body.decision} -> {out['state']}")
    return out


@router.get("/processing-report")
def processing_report(sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """The funnel, replay outcomes, how often reviewers overturn the replay, the cost per processed feedback and the failures. Counts only."""
    if identity.resolve(sess.email, ctx.db).role != "admin":
        raise HTTPException(403, "Only an Admin can see the processing report.")
    with ctx.db.session() as s:
        rows = list(s.scalars(select(Feedback)))
        funnel = {st: 0 for st in feedback.STATES}
        outcomes: dict = {}
        overturned = judged = failures = processed = 0
        for r in rows:
            funnel[r.state] = funnel.get(r.state, 0) + 1
            v = r.verification if isinstance(r.verification, dict) else None
            if r.intent is not None or v is not None:
                processed += 1
            if v and v.get("outcome"):
                outcomes[v["outcome"]] = outcomes.get(v["outcome"], 0) + 1
            if r.review_reason == "error" or (r.last_error and r.state == feedback.CAPTURED):
                failures += 1
            if v and r.reviewer_id is not None and v.get("outcome") in (replay.CONFIRMED, replay.FAILED):
                judged += 1
                if (v["outcome"] == replay.CONFIRMED) == (r.intent == "positive"):
                    overturned += 1
        cost = s.scalar(select(func.coalesce(func.sum(UsageRecord.cost), 0.0)).where(UsageRecord.purpose.in_(("fb_intent", "fb_replay", "fb_ticket")))) or 0.0
        calls = s.scalar(select(func.count()).select_from(UsageRecord).where(UsageRecord.purpose.in_(("fb_intent", "fb_replay", "fb_ticket")))) or 0
    return {"funnel": funnel, "replay_outcomes": outcomes, "reviewed_replays": judged, "overturned": overturned,
            "overturned_share": round(overturned / judged, 3) if judged else None, "processed": processed, "failures": failures,
            "model_calls": calls, "cost": round(cost, 4), "cost_per_feedback": round(cost / processed, 4) if processed else None}
