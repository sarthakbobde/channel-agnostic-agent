"""Feedback processing (Release 3, Phase 3; D51 to D55, D60): the main feedback agent, human review and the handover to ticketing.

The main feedback agent is a RULE-DRIVEN orchestrator. It never chooses a state by model output: it reads the results of the other
agents (intent, verification) and the code decides. Every state change goes through `move()`, one function with a table of allowed
moves and a compare-and-set, so two runners (or a runner and a reviewer) can never both move the same item.

    captured --intent--> no_ticket                      (positive: ends)
    captured --replay--> ready_for_ticket               (mode auto, replay confirmed)
    captured --replay--> human_review                   (review_all mode; replay failed, inconclusive or not possible; an agent is off; errors)
    human_review --a person--> no_ticket | ready_for_ticket
    ready_for_ticket --handover--> ticketed             (ticketing.new: one ticket per feedback)

Every step stores its result on the feedback before the next one starts, so a run that stops half way continues from there, and running a
step twice makes one effect. A step that fails is tried again later with a growing wait; after `feedback_retry_limit` tries the item goes
to a person with the reason `error`, so nothing is stuck. Stored failures are codes, never free text.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update

from app import agent_runtime, feedback, feedback_intent, global_settings, replay, scoped_settings, ticketing
from app.db import Database, Feedback, get_db

log = logging.getLogger(__name__)
CAPTURED, NO_TICKET, HUMAN_REVIEW, READY, TICKETED = feedback.CAPTURED, feedback.NO_TICKET, feedback.HUMAN_REVIEW, feedback.READY_FOR_TICKET, feedback.TICKETED
MODES = ("off", "review_all", "auto")
ALLOWED = {(CAPTURED, NO_TICKET), (CAPTURED, HUMAN_REVIEW), (CAPTURED, READY), (HUMAN_REVIEW, NO_TICKET), (HUMAN_REVIEW, READY), (READY, TICKETED)}
RUN_SECONDS = 600                                    # how long a runner holds a captured item before another may take it
REASONS = ("review_all", "failed", "inconclusive", "not_possible", "daily_cap", "agent_off", "error")


class ReviewError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


def _load(db: Database, fid: int) -> Feedback | None:
    with db.session() as s:
        fb = s.get(Feedback, fid)
        if fb is not None:
            s.expunge(fb)
        return fb


def mode_for(db: Database, fb: Feedback) -> str:
    """The processing mode in effect for this feedback's knowledge base and department (KB, then department, then global)."""
    value = scoped_settings.get("feedback_processing_mode", kb_id=fb.kb_id, department_id=fb.department_id, db=db)
    return value if value in MODES else "off"


# ---- the one function that changes a state ---------------------------------------------------------------------------------------------
def move(db: Database, fid: int, to: str, *, expect: str, **fields) -> bool:
    """captured/human_review/ready_for_ticket -> `to`, only if the item is still in `expect` (compare-and-set). The claim is released.
    Returns False when somebody else moved it first (nothing changes)."""
    if (expect, to) not in ALLOWED:
        raise ValueError(f"A feedback cannot go from {expect} to {to}.")
    values = {"state": to, "claimed_by": "", "claimed_until": None, "updated_at": _now(), **fields}
    with db.session() as s:
        return s.execute(update(Feedback).where(Feedback.id == fid, Feedback.state == expect).values(**values)).rowcount == 1


def _store(db: Database, fid: int, **fields) -> None:
    """A step's result, saved while the item is still `captured` (a reviewer or another runner that moved it keeps priority)."""
    with db.session() as s:
        s.execute(update(Feedback).where(Feedback.id == fid, Feedback.state == CAPTURED).values(updated_at=_now(), **fields))


def _to_review(db: Database, fid: int, reason: str, **fields) -> str:
    return "human_review" if move(db, fid, HUMAN_REVIEW, expect=CAPTURED, review_reason=reason, **fields) else "skipped"


# ---- runs -------------------------------------------------------------------------------------------------------------------------
def _claim(db: Database, fid: int, runner: str) -> bool:
    now = _now()
    with db.session() as s:
        n = s.execute(update(Feedback).where(
            Feedback.id == fid, Feedback.state == CAPTURED, or_(Feedback.claimed_until.is_(None), Feedback.claimed_until < now),
            or_(Feedback.next_attempt_at.is_(None), Feedback.next_attempt_at <= now)).values(claimed_by=runner, claimed_until=now + timedelta(seconds=RUN_SECONDS))).rowcount
    return n == 1


def _retry(db: Database, fid: int, code: str) -> str:
    """A step failed: wait and try again, or after the limit hand the item to a person (D51)."""
    limit = global_settings.current(db).feedback_retry_limit
    with db.session() as s:
        fb = s.get(Feedback, fid)
        attempts = (fb.attempts or 0) + 1 if fb is not None else 1
    if attempts >= limit:
        moved = move(db, fid, HUMAN_REVIEW, expect=CAPTURED, review_reason="error", attempts=attempts, last_error=code[:64], next_attempt_at=None)
        return "human_review" if moved else "skipped"
    wait = min(30, 2 ** attempts)
    with db.session() as s:
        s.execute(update(Feedback).where(Feedback.id == fid, Feedback.state == CAPTURED).values(
            attempts=attempts, last_error=code[:64], next_attempt_at=_now() + timedelta(minutes=wait), claimed_by="", claimed_until=None))
    return "retry"


def replays_today(db: Database) -> int:
    start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    with db.session() as s:
        rows = s.scalars(select(Feedback).where(Feedback.updated_at >= start)).all()
    return sum(1 for r in rows if isinstance(r.verification, dict) and r.verification.get("model_called") and _aware(datetime.fromisoformat(r.verification.get("replayed_at", "1970-01-01T00:00:00+00:00"))) >= start)


def process(db: Database | None, fid: int, runner: str = "") -> str:
    """Run the steps for one `captured` feedback. -> off | skipped | retry | no_ticket | human_review | ticketed | ready_for_ticket."""
    db = db or get_db()
    fb = _load(db, fid)
    if fb is None or fb.state != CAPTURED or mode_for(db, fb) == "off":
        return "off" if fb is not None and fb.state == CAPTURED else "skipped"
    if not _claim(db, fid, runner or "run-" + uuid.uuid4().hex[:8]):
        return "skipped"
    try:
        fb = _load(db, fid)
        mode, g = mode_for(db, fb), global_settings.current(db)
        if mode == "off":
            _store(db, fid, claimed_by="", claimed_until=None)
            return "off"
        if fb.intent is None:                                                     # step 1: intent
            res = feedback_intent.classify(db, fb, replay.rater(db, fb))
            if res.status == agent_runtime.DISABLED:
                return _to_review(db, fid, "agent_off")
            if not res.ok:
                return _retry(db, fid, res.status)
            _store(db, fid, intent=res.output["intent"], intent_source=res.output["source"], summary=res.output["summary"])
            fb = _load(db, fid)
        if fb.intent == feedback_intent.POSITIVE:
            return "no_ticket" if move(db, fid, NO_TICKET, expect=CAPTURED) else "skipped"
        if not isinstance(fb.verification, dict):                                 # step 2: replay and judge
            if not g.feedback_verify_enabled:
                return _to_review(db, fid, "agent_off")
            v = replay.verify(db, fb, may_replay=replays_today(db) < g.feedback_replay_daily_cap)
            _store(db, fid, verification=v)
            fb = _load(db, fid)
        v = fb.verification or {}
        if mode == "review_all":                                                  # step 3: route
            return _to_review(db, fid, "review_all")
        if v.get("outcome") == replay.CONFIRMED:
            if not move(db, fid, READY, expect=CAPTURED):
                return "skipped"
            return handover(db, fid)
        reason = "daily_cap" if v.get("reason") == "daily_cap" else v.get("outcome") if v.get("outcome") in REASONS else "inconclusive"
        return _to_review(db, fid, reason)
    except Exception as e:
        log.warning("feedback %s: a processing step failed (%s)", fid, type(e).__name__)
        return _retry(db, fid, type(e).__name__)


def handover(db: Database | None, fid: int) -> str:
    """ready_for_ticket -> ticketed through the ticketing service (D55). Running it twice makes one ticket. A failure leaves the item in
    ready_for_ticket for the next pass."""
    db = db or get_db()
    fb = _load(db, fid)
    if fb is None:
        return "skipped"
    if fb.state == TICKETED:
        return "ticketed"
    if fb.state != READY:
        return "skipped"
    v = fb.verification if isinstance(fb.verification, dict) else {}
    try:
        ticketing.new(db, fid, replay={k: v.get(k) for k in ("outcome", "reason", "replay_answer", "settings_changed") if v.get(k) is not None} or None)
    except Exception as e:
        log.warning("feedback %s: the handover failed (%s)", fid, type(e).__name__)
        return "ready_for_ticket"
    return "ticketed"


def run_once(db: Database | None = None, limit: int = 20) -> dict:
    """One pass of the job: process eligible captured items, and hand over any ready_for_ticket that is still waiting."""
    db = db or get_db()
    now = _now()
    settled = now - timedelta(minutes=global_settings.current(db).feedback_settle_minutes)       # a comment may still be on its way
    counts: dict = {}
    with db.session() as s:
        captured = list(s.scalars(select(Feedback.id).where(Feedback.state == CAPTURED, Feedback.rating == "down", Feedback.updated_at <= settled,
                                                            or_(Feedback.next_attempt_at.is_(None), Feedback.next_attempt_at <= now)).order_by(Feedback.id).limit(limit)))
        ready = list(s.scalars(select(Feedback.id).where(Feedback.state == READY).order_by(Feedback.id).limit(limit)))
    for fid in captured:
        out = process(db, fid)
        counts[out] = counts.get(out, 0) + 1
    for fid in ready:
        out = handover(db, fid)
        counts[out] = counts.get(out, 0) + 1
    return counts


def job() -> None:
    try:
        run_once()
    except Exception:
        log.warning("the feedback processing job failed", exc_info=True)


def register() -> None:
    """Run the job every minute, under the database lock. It does nothing for a feedback whose processing mode is off."""
    from app.jobs import scheduler
    scheduler.add_job(job, "interval", "feedback-processing", lock_ttl_seconds=600, seconds=60)


# ---- human review (D54) -------------------------------------------------------------------------------------------------------------
def claim_review(db: Database, fid: int, user_id: int) -> dict:
    """A reviewer opens an item: it is held for them for `feedback_review_claim_minutes`, so two people do not work the same one."""
    me, now = f"user:{user_id}", _now()
    minutes = global_settings.current(db).feedback_review_claim_minutes
    with db.session() as s:
        n = s.execute(update(Feedback).where(Feedback.id == fid, Feedback.state == HUMAN_REVIEW, or_(
            Feedback.claimed_until.is_(None), Feedback.claimed_until < now, Feedback.claimed_by == me)).values(
            claimed_by=me, claimed_until=now + timedelta(minutes=minutes))).rowcount
        fb = s.get(Feedback, fid)
        if fb is None:
            raise ReviewError("No such feedback.", 404)
        if n == 1:
            return {"claimed": True, "held_by": me}
        if fb.state != HUMAN_REVIEW:
            raise ReviewError("This item is no longer waiting for a person.")
        return {"claimed": False, "held_by": fb.claimed_by}


def decide(db: Database, fid: int, user_id: int, decision: str, note: str = "") -> dict:
    """A reviewer's decision: positive -> no_ticket, negative -> ready_for_ticket and then the handover. Stored with their id and a masked note.
    Only one decision can win (compare-and-set), and an item held by another reviewer is refused."""
    if decision not in ("positive", "negative"):
        raise ReviewError("Decide positive or negative.", 422)
    fb = _load(db, fid)
    if fb is None:
        raise ReviewError("No such feedback.", 404)
    if fb.state != HUMAN_REVIEW:
        raise ReviewError("This item is no longer waiting for a person.")
    if fb.claimed_by and fb.claimed_by != f"user:{user_id}" and _aware(fb.claimed_until) and _aware(fb.claimed_until) > _now():
        raise ReviewError("Another reviewer is working on this item.")
    clean = ticketing.mask(db, note or "")[:1000]
    to = NO_TICKET if decision == "positive" else READY
    if not move(db, fid, to, expect=HUMAN_REVIEW, intent=decision, intent_source="review", reviewer_id=user_id, reviewed_at=_now(), review_note=clean):
        raise ReviewError("Someone else decided this item first.")
    return {"state": handover(db, fid) if to == READY else to}
