"""Feedback on answers (Release 3, Phase 1; D49, D50).

Each answer can carry rating buttons. A button carries a signed payload (the same scheme as the suggestion buttons, D37): only the
person who asked can rate, and the payload expires. A click is captured as one record per rater and answer, which is updated while
it is still being looked at. The comment is masked before it is stored and attachments are kept as links only. A positive rating
with no comment ends at once (`no_ticket`); everything else waits as `captured` for the processing added in Phase 3.

The records are deleted with their conversation. A failing capture never affects the answer the person already received.
"""
import hmac
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import catalog, global_settings, guardrails, identity, scoped_settings
from app.db import Conversation, Database, Feedback, Message, get_db
from app.suggestions import PayloadError, _b64, _keys, _unb64, asker_key, channel_of

log = logging.getLogger(__name__)
ACTION = "rate"
UP, DOWN = "up", "down"
CAPTURED, NO_TICKET, HUMAN_REVIEW, READY_FOR_TICKET, TICKETED = "captured", "no_ticket", "human_review", "ready_for_ticket", "ticketed"
STATES = (CAPTURED, NO_TICKET, HUMAN_REVIEW, READY_FOR_TICKET, TICKETED)
OPEN_STATES = (CAPTURED, NO_TICKET, HUMAN_REVIEW)          # a rating changes the same record while it is in one of these (D49)
REASONS = global_settings.FEEDBACK_REASONS
MAX_ATTACHMENTS, MAX_LINK = 5, 500
_LINK = re.compile(r"^https?://[^\s<>\"']+$", re.I)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enabled(conv_id: str | None, kbs: list[dict]) -> bool:
    """On only when the channel and at least one of the knowledge bases asked are switched on (both off by default)."""
    try:
        if not conv_id or not kbs:
            return False
        if not scoped_settings.get("feedback_enabled", channel=channel_of(conv_id)):
            return False
        return any(scoped_settings.for_kb(kb, "feedback_enabled") for kb in kbs)
    except Exception:
        log.warning("could not read the feedback settings; no rating buttons are offered", exc_info=True)
        return False


def reasons_offered() -> list[str]:
    chosen = [r for r in global_settings.current().feedback_reasons if r in REASONS]
    return chosen or list(REASONS)


# ---- signed payloads (D37, D50) ----------------------------------------------------------------------------------------------
def sign(answer_id: int, asker: str, expires: datetime) -> str | None:
    keys = _keys()
    if not keys:
        return None                    # no master key: no rating buttons
    body = _b64(json.dumps({"v": 1, "act": ACTION, "a": answer_id, "u": asker, "x": int(expires.timestamp())}, separators=(",", ":")).encode())
    import hashlib
    return body + "." + _b64(hmac.new(keys[0], body.encode(), hashlib.sha256).digest())


def is_rating(payload: str) -> bool:
    """Whether a button payload is a rating one (read without trusting it: verify() does the checking)."""
    try:
        return json.loads(_unb64((payload or "").split(".", 1)[0])).get("act") == ACTION
    except Exception:
        return False


def verify(payload: str, clicker: str) -> dict:
    """The payload's fields, after checking its signature, its expiry and that `clicker` (an asker key) is who asked."""
    import hashlib
    try:
        body, sig = (payload or "").split(".", 1)
        given = _unb64(sig)
    except Exception:
        raise PayloadError("feedback_invalid") from None
    if not any(hmac.compare_digest(given, hmac.new(k, body.encode(), hashlib.sha256).digest()) for k in _keys()):
        raise PayloadError("feedback_invalid")
    try:
        data = json.loads(_unb64(body))
    except Exception:
        raise PayloadError("feedback_invalid") from None
    if data.get("act") != ACTION or data.get("v") != 1 or "a" not in data or "x" not in data:
        raise PayloadError("feedback_invalid")
    if data["x"] < time.time():
        raise PayloadError("feedback_expired")
    if not hmac.compare_digest(str(data.get("u", "")), clicker):
        raise PayloadError("feedback_not_yours")
    return data


def controls(answer_id: int | None, asker: str, lang: str = "", shown_at: datetime | None = None) -> dict | None:
    """What a presenter shows under an answer: {payload, labels}. None when there is nothing to rate or no key to sign with."""
    if not answer_id:
        return None
    until = (shown_at or _now()) + timedelta(days=global_settings.current().feedback_payload_days)
    payload = sign(answer_id, asker, until)
    if not payload:
        return None
    return {"payload": payload, "answer_id": answer_id, "reasons": reasons_offered(), "labels": labels(lang)}


def labels(lang: str = "") -> dict:
    """The texts of the rating controls, in the answer's language."""
    names = ("prompt", "up", "down", "reason", "comment", "submit")
    out = {n: catalog.t("feedback_" + n, lang) for n in names}
    out["reason_labels"] = {r: catalog.t("feedback_reason_" + r, lang) for r in REASONS}
    return out


# ---- capture -----------------------------------------------------------------------------------------------------------------
def clean_links(raw) -> list[str]:
    """Attachments are kept as links only: anything that is not an http(s) link is dropped, and nothing is downloaded."""
    out = []
    for x in raw or []:
        if isinstance(x, str) and len(x) <= MAX_LINK and _LINK.match(x.strip()) and x.strip() not in out:
            out.append(x.strip())
    return out[:MAX_ATTACHMENTS]


def _masked_comment(text: str, db: Database) -> str:
    text = " ".join((text or "").split())[:global_settings.current(db).feedback_comment_max_chars]
    if not text:
        return ""
    decision = guardrails.mask_input(text, db)
    if decision.blocked:
        raise RuntimeError("the comment could not be masked")     # fails closed, like a question
    return decision.text


def capture(data: dict, rating: str, reason: str = "", comment: str = "", attachments=None, *, who=None, db: Database | None = None) -> dict:
    """Store a rating. `data` comes from verify(). Returns {id, state, intent, created, changed}. Raises ValueError for input the
    caller got wrong (an unknown rating or reason, an answer that is not there)."""
    db = db or get_db()
    rating = (rating or "").strip().lower()
    if rating not in (UP, DOWN):
        raise ValueError("rating")
    reason = (reason or "").strip().lower()
    if reason and reason not in reasons_offered():
        raise ValueError("reason")
    if rating == UP:
        reason = ""                                    # a reason belongs to a complaint
    comment = _masked_comment(comment, db)
    links = clean_links(attachments)
    who = who or identity.current()
    now = _now()
    with db.session() as s:
        msg = s.get(Message, data["a"])
        if msg is None or msg.role != "assistant":
            raise ValueError("answer")
        conv = s.get(Conversation, msg.conversation_id)
        kb_ids = (msg.meta or {}).get("kb_ids") or []
        row = s.scalars(select(Feedback).where(Feedback.rater_key == data["u"], Feedback.answer_id == data["a"],
                                               Feedback.state.in_(OPEN_STATES)).order_by(Feedback.id.desc())).first()
        created = row is None
        if row is None:
            row = Feedback(answer_id=data["a"], conversation_id=msg.conversation_id, rater_key=data["u"],
                           user_id=who.user_id or (conv.owner_user_id if conv else None), kb_id=kb_ids[0] if kb_ids else None,
                           department_id=conv.department_id if conv else None, channel=conv.channel if conv else "other",
                           rating=rating, reason=reason, comment=comment, attachments=links, state=CAPTURED, created_at=now, updated_at=now)
            s.add(row)
        before = (row.rating, row.reason, row.comment, list(row.attachments or []))
        row.rating = rating
        row.reason = reason if (reason or rating == UP or created) else row.reason
        if comment or created:
            row.comment = comment or row.comment
        if links:
            row.attachments = links
        changed = created or before != (row.rating, row.reason, row.comment, list(row.attachments or []))
        if row.state != HUMAN_REVIEW:                  # a person's review is never undone by a later click
            if row.rating == UP and not row.comment:
                row.state, row.intent, row.intent_source = NO_TICKET, "positive", "rating"
            else:
                row.state, row.intent, row.intent_source = CAPTURED, None, None
        if changed:
            row.updated_at = now
        s.flush()
        return {"id": row.id, "state": row.state, "intent": row.intent, "created": created, "changed": changed}


class FeedbackOff(Exception):
    """Rating is not switched on for this answer's channel and knowledge bases."""


def authorize_answer(answer_id: int, conv_id: str, asker: str, db: Database | None = None) -> dict:
    """The fields verify() gives, for a person signed in to the Web UI who rates through the page's own thumbs (no button payload):
    the answer must belong to this conversation, and rating must be on for its channel and knowledge bases. The caller has already
    been authenticated, and `conv_id` is the conversation id under their own prefix, so only the asker can reach it (D50)."""
    from app.knowledge import kbs
    db = db or get_db()
    with db.session() as s:
        msg = s.get(Message, answer_id)
        if msg is None or msg.role != "assistant" or msg.conversation_id != conv_id:
            raise PayloadError("feedback_invalid")
        kb_ids = list((msg.meta or {}).get("kb_ids") or [])
    targets = [k for k in (kbs.get_kb(db, i) for i in kb_ids) if k]
    if not enabled(conv_id, targets):
        raise FeedbackOff()
    return {"v": 1, "act": ACTION, "a": answer_id, "u": asker, "x": int(time.time()) + 60}
