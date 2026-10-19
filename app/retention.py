"""Conversation retention: delete conversations whose last activity is older than their department's retention.

A department's retention is the number of days it chose (never more than the global maximum). A conversation belongs to the
department of the person who started it; conversations with no department (or a department that was deleted) use the default
department's. The daily job and "Run now" both record what they did in the audit log, with counts and never any text.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select

from app import global_settings
from app.db import Conversation, Database, Department, Message, get_db

log = logging.getLogger(__name__)
ACTOR = "system (retention job)"


def _departments(db: Database):
    with db.session() as s:
        rows = [(d.id, d.name, d.retention_days, d.is_default) for d in s.scalars(select(Department))]
    cap = global_settings.current(db).max_retention_days
    default = next((r for r in rows if r[3]), rows[0] if rows else None)
    return rows, cap, default


def _expired_query(dept_id: int, days: int, known: list[int], is_default: bool, now: datetime):
    cutoff = now - timedelta(days=days)
    cond = Conversation.department_id == dept_id
    if is_default:
        cond = cond | Conversation.department_id.is_(None) | ~Conversation.department_id.in_(known)
    return select(Conversation.id).where(cond, Conversation.updated_at < cutoff)


def plan(db: Database | None = None, now: datetime | None = None) -> list[dict]:
    """What a run would delete now, per department. Deletes nothing."""
    db = db or get_db()
    now = now or datetime.now(timezone.utc)
    rows, cap, default = _departments(db)
    known = [r[0] for r in rows]
    out = []
    with db.session() as s:
        for dept_id, name, days, is_default in rows:
            days = max(1, min(days, cap))
            q = _expired_query(dept_id, days, known, is_default, now)
            out.append({"department": name, "days": days, "expired": s.scalar(select(func.count()).select_from(q.subquery())) or 0})
    return out


def run(db: Database | None = None, actor: str = ACTOR, now: datetime | None = None) -> dict:
    """Delete expired conversations (and their messages). Audited. Returns the counts."""
    db = db or get_db()
    now = now or datetime.now(timezone.utc)
    rows, cap, default = _departments(db)
    known = [r[0] for r in rows]
    per_dept, conversations, messages = {}, 0, 0
    for dept_id, name, days, is_default in rows:
        days = max(1, min(days, cap))
        with db.session() as s:
            ids = list(s.scalars(_expired_query(dept_id, days, known, is_default, now)))
            if not ids:
                continue
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                messages += s.execute(delete(Message).where(Message.conversation_id.in_(chunk))).rowcount or 0
                s.execute(delete(Conversation).where(Conversation.id.in_(chunk)))
        per_dept[name] = len(ids)
        conversations += len(ids)
    result = {"conversations": conversations, "messages": messages, "by_department": per_dept}
    _audit(actor, "retention.run", "conversations",
           f"deleted {conversations} conversations ({messages} messages)" + (": " + ", ".join(f"{k} {v}" for k, v in per_dept.items()) if per_dept else "")
           + "; limits: " + ", ".join(f"{r[1]} {max(1, min(r[2], cap))} days" for r in rows))
    log.info("retention run: %s", result)
    return result


def _audit(actor: str, action: str, target: str, detail: str) -> None:
    try:
        from app.channels import get_store
        store = get_store()
        if store:
            store.record(actor, action, target, detail)
    except Exception:
        log.warning("could not audit the retention run", exc_info=True)


def job() -> None:
    """The scheduled daily job. Reads the switch each time, so it can be turned off without a restart."""
    db = get_db()
    if not global_settings.current(db).retention_job_enabled:
        _audit(ACTOR, "retention.run", "conversations", "skipped: the daily job is turned off in Settings")
        return
    run(db)


def register() -> None:
    """Schedule the daily job (03:30 UTC)."""
    from app.jobs import scheduler
    scheduler.add_job(job, "cron", "retention-daily", lock_ttl_seconds=900, hour=3, minute=30)
