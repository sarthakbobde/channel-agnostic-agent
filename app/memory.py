"""Conversation memory, kept in the database.

A turn is saved in two steps so it can never be lost: the (masked) question is stored first, together with an answer row
marked incomplete; when the answer finishes that row is filled in and marked complete. If the process is killed or the
model fails part-way, the question is still there and the answer stays incomplete (with whatever text had arrived).
Everything stored is the masked text from the guardrails. The old per-thread text files are imported once by
`import_files` and are not written to any more.
"""
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app import global_settings
from app.config import settings
from app.db import Conversation, Database, Message, get_db

log = logging.getLogger(__name__)
INTERRUPTED = "[The answer was interrupted before it finished.]"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _channel(conv_id: str) -> str:
    head = conv_id.split("-", 1)[0].lower()
    return head if head in ("web", "slack", "teams", "gchat") else "other"


@dataclass(frozen=True)
class Turn:
    conversation_id: str
    question_id: int
    answer_id: int


def _ensure(s, conv_id: str, who) -> Conversation:
    conv = s.get(Conversation, conv_id)
    if conv is None:
        conv = Conversation(id=conv_id, channel=_channel(conv_id), owner_user_id=getattr(who, "user_id", None),
                            owner_email=getattr(who, "email", "") or "", department=getattr(who, "department", "") or "",
                            department_id=getattr(who, "department_id", None), title="")
        s.add(conv)
        s.flush()
    return conv


def _store(s, conv_id: str, who, role: str, content: str, meta: dict | None, complete: bool, ts=None) -> Message:
    conv = _ensure(s, conv_id, who)
    msg = Message(conversation_id=conv_id, role=role, content=content, meta=meta or {}, complete=complete, ts=ts or _now())
    s.add(msg)
    if role == "user" and not conv.title:
        conv.title = content.replace("\n", " ")[:50]
    conv.updated_at = msg.ts
    s.flush()
    return msg


def _write(conv_id: str, who, db: Database | None, fn):
    """Run a write, retrying once if two writers created the same conversation at the same moment."""
    db = db or get_db()
    for attempt in (1, 2):
        try:
            with db.session() as s:
                return fn(s)
        except IntegrityError:
            if attempt == 2:
                raise


# ---- saving a turn --------------------------------------------------------------------------------------
def begin_turn(conv_id: str, question: str, who=None, db: Database | None = None) -> Turn:
    """Store the question now, and an empty answer row marked incomplete."""
    def go(s):
        q = _store(s, conv_id, who, "user", question, None, True)
        a = _store(s, conv_id, who, "assistant", "", None, False)
        return Turn(conv_id, q.id, a.id)
    return _write(conv_id, who, db, go)


def finish_turn(turn: Turn, answer: str, meta: dict | None = None, db: Database | None = None) -> None:
    with (db or get_db()).session() as s:
        m = s.get(Message, turn.answer_id)
        if m is not None:
            m.content, m.meta, m.complete = answer, meta or {}, True


def fail_turn(turn: Turn, partial: str = "", db: Database | None = None) -> None:
    """The answer did not finish: keep what arrived and leave it marked incomplete."""
    try:
        with (db or get_db()).session() as s:
            m = s.get(Message, turn.answer_id)
            if m is not None and not m.complete:
                m.content = partial
    except Exception:
        log.warning("could not record an interrupted answer", exc_info=True)


def add(conv_id: str, role: str, content: str, meta: dict | None = None, who=None, db: Database | None = None) -> None:
    """Append one finished message (used by the file import and by tests)."""
    _write(conv_id, who, db, lambda s: _store(s, conv_id, who, role, content, meta, True))


# ---- reading ------------------------------------------------------------------------------------------------
def get(conv_id: str, db: Database | None = None) -> list[tuple[str, str]]:
    """The last N turns as (role, content), oldest first, for the model. A question whose answer never finished is left out."""
    limit = global_settings.current().recent_turns * 2
    with (db or get_db()).session() as s:
        rows = list(s.scalars(select(Message).where(Message.conversation_id == conv_id).order_by(Message.id.desc()).limit(limit + 4)))
    rows.reverse()
    out: list = []
    for m in rows:
        if m.role == "assistant" and not m.complete:
            if out and out[-1][0] == "user":
                out.pop()
            continue
        out.append((m.role, m.content))
    return out[-limit:]


def get_summary(conv_id: str, db: Database | None = None) -> str:
    with (db or get_db()).session() as s:
        c = s.get(Conversation, conv_id)
        return c.summary if c is not None else ""


def get_context(conv_id: str, db: Database | None = None) -> tuple[str, list[tuple[str, str]]]:
    """(summary of the older turns, the most recent turns word for word): what the model is given about this conversation."""
    return get_summary(conv_id, db), get(conv_id, db)


def get_messages(conv_id: str, db: Database | None = None) -> list[dict]:
    """Full history for display."""
    with (db or get_db()).session() as s:
        rows = list(s.scalars(select(Message).where(Message.conversation_id == conv_id).order_by(Message.id)))
        return [{"role": m.role, "content": m.content if (m.complete or m.content) else INTERRUPTED,
                 "meta": m.meta or {}, **({} if m.complete else {"incomplete": True})} for m in rows]


def list_conversations(prefix: str = "", db: Database | None = None) -> list[dict]:
    """Newest first. `prefix` limits to one person's conversations."""
    with (db or get_db()).session() as s:
        q = select(Conversation).order_by(Conversation.updated_at.desc())
        if prefix:
            q = q.where(Conversation.id.like(prefix.replace("%", "").replace("_", "\\_") + "%", escape="\\"))
        return [{"id": c.id, "title": c.title or "New chat", "updated_at": c.updated_at.isoformat() if c.updated_at else None}
                for c in s.scalars(q)]


def delete(conv_id: str, db: Database | None = None) -> bool:
    with (db or get_db()).session() as s:
        n = s.execute(sql_delete(Message).where(Message.conversation_id == conv_id)).rowcount
        c = s.get(Conversation, conv_id)
        if c is not None:
            s.delete(c)
        return bool(c is not None or n)


clear = delete


# ---- one-off import of the old text files ----------------------------------------------------------------------
def import_files(db: Database | None = None, directory: str | None = None) -> dict:
    """Bring the old per-thread text files into the database. Repeatable: a conversation that is already there is skipped.
    Old text may predate the guardrails, so personal data is masked on the way in. The files are left in place as a backup."""
    db = db or get_db()
    folder = Path(directory or settings.chat_dir)
    done = skipped = 0
    if not folder.is_dir():
        return {"imported": 0, "skipped": 0}
    from app import guardrails
    try:
        g = guardrails.current(db)
        mask = g.enabled and g.mask_enabled
    except Exception:
        g, mask = None, False
    for f in sorted(folder.glob("*.txt")):
        conv_id = f.stem
        with db.session() as s:
            if s.get(Conversation, conv_id) is not None:
                skipped += 1
                continue
        records = []
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and rec.get("role") in ("user", "assistant"):
                records.append(rec)
        if not records:
            continue
        m = re.match(r"web-(\d+)-", conv_id)
        owner = int(m.group(1)) if m else None
        who = _OwnerStub(owner, db)
        try:
            with db.session() as s:
                for rec in records:
                    text = str(rec.get("content", ""))
                    if mask and text:
                        text, _ = guardrails.mask(text, g.masked_types, g.mask_names)
                    ts = datetime.fromtimestamp(float(rec.get("ts", 0)), timezone.utc) if rec.get("ts") else _now()
                    _store(s, conv_id, who, rec["role"], text, rec.get("meta") if isinstance(rec.get("meta"), dict) else None, True, ts)
            done += 1
        except Exception:
            log.warning("could not import %s", f.name, exc_info=True)
    return {"imported": done, "skipped": skipped}


class _OwnerStub:
    """Owner details for an imported conversation, looked up from the user id in the old file name."""
    def __init__(self, user_id, db):
        self.user_id, self.email, self.department, self.department_id = user_id, "", "", None
        if user_id is not None:
            try:
                from app.db import Department, User
                with db.session() as s:
                    u = s.get(User, user_id)
                    if u is not None:
                        d = s.get(Department, u.department_id)
                        self.email, self.department_id, self.department = u.email, u.department_id, d.name if d else ""
            except Exception:
                log.warning("could not look up the owner of an imported conversation", exc_info=True)
