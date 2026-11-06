"""Feedback on answers (Release 3, Phase 1; D49, D50, D61).

Each answer can carry rating buttons. A button carries a signed payload (the same scheme as the suggestion buttons, D37): only the
person who asked can rate, and the payload expires. A click is captured as one open record per rater and answer, which is updated until
it is ticketed. The comment is masked before it is stored (always, unless an Admin sets `feedback_masking` to follow the switch, D61)
and attachments are kept as links only. A positive rating with no comment ends at once (`no_ticket`); everything else waits as
`captured` for the processing added in Phase 3. There is no reason list (D50): a feedback is a rating, a comment and links.

A conversation that is deleted keeps its feedback, and the feedback keeps pointing at the moved-aside conversation (D62, see memory.py).
A failing capture never affects the answer the person already received.
"""
import hmac
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import catalog, global_settings, guardrails, identity, scoped_settings
from app.db import Conversation, Database, Feedback, Message, get_db
from app.suggestions import PayloadError, _b64, _keys, _unb64, asker_key, channel_of

log = logging.getLogger(__name__)
ACTION = "rate"
UP, DOWN = "up", "down"
CAPTURED, NO_TICKET, HUMAN_REVIEW, READY_FOR_TICKET, TICKETED = "captured", "no_ticket", "human_review", "ready_for_ticket", "ticketed"
STATES = (CAPTURED, NO_TICKET, HUMAN_REVIEW, READY_FOR_TICKET, TICKETED)
OPEN_STATES = (CAPTURED, NO_TICKET, HUMAN_REVIEW, READY_FOR_TICKET)        # any state but ticketed: one open record per rater and answer (D49)
PROTECTED_STATES = (HUMAN_REVIEW, READY_FOR_TICKET)                         # a later click never moves a record out of these
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
    return {"payload": payload, "answer_id": answer_id, "labels": labels(lang)}


def labels(lang: str = "") -> dict:
    """The texts of the rating controls, in the answer's language."""
    return {n: catalog.t("feedback_" + n, lang) for n in ("prompt", "up", "down", "comment", "link", "submit", "thanks")}


def form_for(payload: str, answer_id: int, lang: str = "") -> dict:
    """What a presenter asks for after a thumbs-down that came without a comment: a comment and an optional link."""
    return {"payload": payload, "answer_id": answer_id, "labels": labels(lang)}


# ---- capture -----------------------------------------------------------------------------------------------------------------
def clean_links(raw) -> list[str]:
    """Attachments are kept as links only: anything that is not an http(s) link is dropped, and nothing is downloaded."""
    out = []
    for x in raw or []:
        if isinstance(x, str) and len(x) <= MAX_LINK and _LINK.match(x.strip()) and x.strip() not in out:
            out.append(x.strip())
    return out[:MAX_ATTACHMENTS]


def _masked_comment(text: str, db: Database) -> str:
    """Cut to the limit, then masked. Always (D61), unless an Admin set `feedback_masking` to `follow_switch` and the question-masking
    switch is off. If masking fails the comment is refused, never stored unmasked."""
    snap = global_settings.current(db)
    text = " ".join((text or "").split())[:snap.feedback_comment_max_chars]
    if not text:
        return ""
    try:
        g = guardrails.current(db)
        if snap.feedback_masking == "follow_switch" and not g.mask_enabled:
            return text
        masked, counts = guardrails.mask_with(text, g)
        if counts:
            guardrails.record(db, "personal_data", "input", "masked", sum(counts.values()))
        return masked
    except Exception as e:
        raise RuntimeError("the comment could not be masked") from e


def _open_row(s, data: dict):
    return s.scalars(select(Feedback).where(Feedback.rater_key == data["u"], Feedback.answer_id == data["a"],
                                            Feedback.state.in_(OPEN_STATES)).order_by(Feedback.id.desc())).first()


def capture(data: dict, rating: str, comment: str = "", attachments=None, *, who=None, db: Database | None = None) -> dict:
    """Store a rating. `data` comes from verify(). Returns {id, state, intent, created, changed}. Raises ValueError for input the
    caller got wrong (an unknown rating, an answer that is not there). Two clicks at once cannot make two open records: the database
    refuses the second (a partial unique index), and it is then stored as an update of the first."""
    db = db or get_db()
    rating = (rating or "").strip().lower()
    if rating not in (UP, DOWN):
        raise ValueError("rating")
    comment = _masked_comment(comment, db)
    links = clean_links(attachments)
    who = who or identity.current()
    for attempt in (1, 2):
        try:
            return _store(db, data, rating, comment, links, who)
        except IntegrityError:
            if attempt == 2:
                raise
            log.info("two ratings of the same answer arrived at once; the second updates the first")


def _store(db: Database, data: dict, rating: str, comment: str, links: list[str], who) -> dict:
    now = _now()
    with db.session() as s:
        msg = s.get(Message, data["a"])
        if msg is None or msg.role != "assistant":
            raise ValueError("answer")
        conv = s.get(Conversation, msg.conversation_id)
        kb_ids = (msg.meta or {}).get("kb_ids") or []
        row = _open_row(s, data)
        created = row is None
        if row is None:
            row = Feedback(answer_id=data["a"], conversation_id=msg.conversation_id, rater_key=data["u"],
                           user_id=who.user_id or (conv.owner_user_id if conv else None), kb_id=kb_ids[0] if kb_ids else None,
                           department_id=conv.department_id if conv else None, channel=conv.channel if conv else "other",
                           rating=rating, comment=comment, attachments=links, state=CAPTURED, created_at=now, updated_at=now)
            s.add(row)
        before = (row.rating, row.comment, list(row.attachments or []))
        row.rating = rating
        if comment or created:
            row.comment = comment or row.comment
        if links:
            row.attachments = links
        changed = created or before != (row.rating, row.comment, list(row.attachments or []))
        if row.state not in PROTECTED_STATES:          # a review in progress, or a feedback about to be ticketed, is never undone by a click
            if row.rating == UP and not row.comment:
                row.state, row.intent, row.intent_source = NO_TICKET, "positive", "rating"
            else:
                row.state, row.intent, row.intent_source = CAPTURED, None, None
        if changed:
            row.updated_at = now
        s.flush()
        return {"id": row.id, "state": row.state, "intent": row.intent, "created": created, "changed": changed}
