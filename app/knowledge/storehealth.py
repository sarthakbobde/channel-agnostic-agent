"""Store health: a periodic ping of every knowledge base's active vector store.

An unreachable store raises an Admin alert (shown as a banner); it clears itself when the store answers again.
Alert text never contains a connection string or key.
"""
import logging

from app import alerts
from app.db import Database, get_db
from app.knowledge import kbs

log = logging.getLogger(__name__)
INTERVAL_MINUTES = 5


def check_all(db: Database | None = None) -> dict:
    db = db or get_db()
    result = {}
    for kb in kbs.list_kbs(db):
        key = f"store_unreachable:{kb['id']}"
        try:
            ok, message = kbs.store_for(kb).health()
        except Exception as e:
            ok, message = False, type(e).__name__
        result[kb["name"]] = ok
        try:
            if ok:
                alerts.resolve_key(db, key)
            else:
                alerts.raise_alert(db, key, "store_unreachable",
                                   f"The vector store of '{kb['name']}' is not answering ({message}). Questions to this knowledge base will find nothing until it is back.", "error")
        except Exception:
            log.warning("could not update the store alert", exc_info=True)
    return result


def register() -> None:
    from app.jobs import scheduler
    scheduler.add_job(check_all, "interval", "store-health", lock_ttl_seconds=240, minutes=INTERVAL_MINUTES)
