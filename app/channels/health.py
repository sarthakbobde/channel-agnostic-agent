"""Channel health: every few minutes check that each switched-on channel is running and connected.

A channel that is on but not running, or whose connection to the platform dropped, raises an Admin alert that clears itself
when the channel is healthy again. One broken channel never affects the others.
"""
import logging

from app import alerts
from app.db import Database, get_db

log = logging.getLogger(__name__)
INTERVAL_MINUTES = 2


def check_all(db: Database | None = None, mgr=None, store=None) -> dict:
    from app.channels import get_store
    from app.channels.manager import manager
    db, mgr, store = db or get_db(), mgr or manager, store or get_store()
    result = {}
    if store is None:
        return result
    for typ in mgr.registry:
        if typ == "web":
            continue
        try:
            found = store.runtime_config(typ)
        except Exception:
            continue
        if not found or not found[0]:
            alerts.resolve_key(db, f"channel_down:{typ}")                  # off or not set up: nothing to complain about
            continue
        adapter = mgr.get(typ)
        title = mgr.adapter_class(typ).title or typ
        if adapter is None:
            problem = f"{title} is switched on but is not running."
        else:
            try:
                healthy = adapter.healthy()
            except Exception:
                healthy = False
            problem = "" if healthy else f"{title} lost its connection to the platform."
        result[typ] = not problem
        try:
            if problem:
                alerts.raise_alert(db, f"channel_down:{typ}", "channel_down", problem + " People cannot reach the assistant there until it is back.", "error")
            else:
                alerts.resolve_key(db, f"channel_down:{typ}")
        except Exception:
            log.warning("could not update the channel alert", exc_info=True)
    return result


def register() -> None:
    from app.jobs import scheduler
    scheduler.add_job(check_all, "interval", "channel-health", lock_ttl_seconds=90, minutes=INTERVAL_MINUTES)
