"""Scheduler scaffolding for background jobs (retention, store health checks, ...).

v1 runs as a single instance (decision D11). The database lock below makes a job run in only one
process at a time even if a second copy is started by mistake: whoever holds an unexpired lock
runs the job; the others skip that run. Scaling out for real would need leader election.

No jobs are registered yet; later phases call `add_job`.
"""
import logging
import os
import socket
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError

from app.db import Database, JobLock, get_db

log = logging.getLogger(__name__)
INSTANCE = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"

_scheduler: BackgroundScheduler | None = None
_guard = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---- database lock -----------------------------------------------------------------
def acquire_lock(name: str, ttl_seconds: float, db: Database | None = None, owner: str = INSTANCE) -> bool:
    """Take (or renew) the lock `name` for `ttl_seconds`. False if someone else holds it unexpired."""
    db = db or get_db()
    now = _utcnow()
    until = now + timedelta(seconds=ttl_seconds)
    with db.session() as s:
        taken = s.execute(
            update(JobLock)
            .where(JobLock.name == name, (JobLock.locked_until <= now) | (JobLock.owner == owner))
            .values(owner=owner, locked_until=until)
        ).rowcount
        if taken:
            return True
        try:
            s.add(JobLock(name=name, owner=owner, locked_until=until))
            s.flush()
            return True
        except IntegrityError:                     # a live lock held by another owner
            s.rollback()
            return False


def release_lock(name: str, db: Database | None = None, owner: str = INSTANCE) -> None:
    db = db or get_db()
    with db.session() as s:
        s.execute(delete(JobLock).where(JobLock.name == name, JobLock.owner == owner))


def run_locked(name: str, ttl_seconds: float, fn: Callable[[], None], db: Database | None = None) -> bool:
    """Run `fn` only if this process gets the lock. Returns whether it ran."""
    if not acquire_lock(name, ttl_seconds, db):
        log.info("job %s skipped: another process holds the lock", name)
        return False
    try:
        fn()
        return True
    except Exception:
        log.exception("job %s failed", name)
        return True
    finally:
        release_lock(name, db)


# ---- scheduler ---------------------------------------------------------------------
def start() -> BackgroundScheduler:
    global _scheduler
    with _guard:
        if _scheduler is None or not _scheduler.running:
            _scheduler = BackgroundScheduler(timezone="UTC")
            _scheduler.start()
            log.info("scheduler started (no jobs registered yet)")
        return _scheduler


def stop() -> None:
    global _scheduler
    with _guard:
        if _scheduler is not None and _scheduler.running:
            _scheduler.shutdown(wait=False)
        _scheduler = None


def add_job(fn: Callable[[], None], trigger: str, name: str, lock_ttl_seconds: float = 300,
            db: Database | None = None, **trigger_args):
    """Schedule `fn` (for example trigger='cron', hour=3). It runs under the database lock."""
    scheduler = start()
    return scheduler.add_job(lambda: run_locked(name, lock_ttl_seconds, fn, db), trigger, id=name,
                             replace_existing=True, **trigger_args)
