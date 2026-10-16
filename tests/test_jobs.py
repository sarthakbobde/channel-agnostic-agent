"""Scheduler scaffolding and the database job lock (decision D11)."""
import threading
import time

import pytest

from app.jobs import scheduler


@pytest.fixture(autouse=True)
def stopped():
    scheduler.stop()
    yield
    scheduler.stop()


def test_a_second_owner_cannot_take_a_live_lock(new_db):
    db = new_db()
    assert scheduler.acquire_lock("retention", 60, db, owner="A")
    assert not scheduler.acquire_lock("retention", 60, db, owner="B")
    assert scheduler.acquire_lock("retention", 60, db, owner="A")            # the holder can renew


def test_a_lock_is_free_after_release_or_expiry(new_db):
    db = new_db()
    assert scheduler.acquire_lock("job", 60, db, owner="A")
    scheduler.release_lock("job", db, owner="B")                             # not the owner: no effect
    assert not scheduler.acquire_lock("job", 60, db, owner="B")
    scheduler.release_lock("job", db, owner="A")
    assert scheduler.acquire_lock("job", 60, db, owner="B")
    assert scheduler.acquire_lock("short", 0.05, db, owner="A")
    time.sleep(0.15)
    assert scheduler.acquire_lock("short", 60, db, owner="B")                # expired, taken over


def test_locks_are_independent_per_job_name(new_db):
    db = new_db()
    assert scheduler.acquire_lock("one", 60, db, owner="A")
    assert scheduler.acquire_lock("two", 60, db, owner="B")


def test_run_locked_skips_when_another_process_holds_the_lock(new_db):
    db = new_db()
    ran = []
    assert scheduler.acquire_lock("job", 60, db, owner="someone-else")
    assert scheduler.run_locked("job", 60, lambda: ran.append(1), db) is False and ran == []
    scheduler.release_lock("job", db, owner="someone-else")
    assert scheduler.run_locked("job", 60, lambda: ran.append(1), db) is True and ran == [1]
    assert scheduler.acquire_lock("job", 60, db, owner="B")                  # run_locked released it


def test_a_failing_job_is_logged_and_releases_its_lock(new_db, caplog):
    db = new_db()

    def boom():
        raise RuntimeError("nope")
    assert scheduler.run_locked("job", 60, boom, db) is True
    assert "job job failed" in caplog.text
    assert scheduler.acquire_lock("job", 60, db, owner="B")


def test_scheduler_starts_stops_and_runs_a_job_under_the_lock(new_db):
    db = new_db()
    assert scheduler.start() is scheduler.start()                           # idempotent
    done = threading.Event()
    scheduler.add_job(done.set, "interval", name="tick", db=db, seconds=0.1)
    assert done.wait(5), "scheduled job never ran"
    scheduler.stop()
    scheduler.stop()                                                         # stopping twice is fine
    assert scheduler._scheduler is None
