"""Adapter registry. Add a platform = add a module + one line here."""
import importlib
import logging
from app.channels.base import Adapter
from app.channels.config import load

log = logging.getLogger(__name__)
REGISTRY = {
    "slack": ("app.channels.slack", "SlackAdapter"),
    "teams": ("app.channels.teams", "TeamsAdapter"),
    "gchat": ("app.channels.gchat", "GChatAdapter"),
}
_adapters: list[Adapter] = []


def mount(app) -> None:
    """Build every enabled adapter; attach webhook routers to the FastAPI app."""
    cfg = load()
    for name, ccfg in cfg["channels"].items():
        if not ccfg.get("enabled"):
            continue
        if name not in REGISTRY:
            log.warning("unknown channel %r in channels.yaml, skipped", name)
            continue
        mod, cls = REGISTRY[name]
        try:
            adapter = getattr(importlib.import_module(mod), cls)({**cfg["defaults"], **ccfg})
            router = adapter.router()
            if router is not None:
                app.include_router(router)
            _adapters.append(adapter)
        except Exception:
            log.exception("failed to set up channel %s", name)


def start_all() -> None:
    for a in _adapters:
        try:
            a.start()
        except Exception:
            log.exception("failed to start channel %s", a.name)


def stop_all() -> None:
    for a in _adapters:
        try:
            a.stop()
        except Exception:
            log.exception("failed to stop %s", a.name)
