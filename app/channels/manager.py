"""Runs channel adapters and lets them be started, replaced and stopped at runtime."""
import importlib
import logging
import threading
from fastapi import HTTPException, Request
from app.channels.base import Adapter
from app.channels.config import load

log = logging.getLogger(__name__)

REGISTRY = {
    "slack": ("app.channels.slack", "SlackAdapter"),
    "teams": ("app.channels.teams", "TeamsAdapter"),
    "gchat": ("app.channels.gchat", "GChatAdapter"),
}


class ChannelManager:
    def __init__(self, registry: dict | None = None):
        self.registry = dict(registry or REGISTRY)
        self._running: dict[str, Adapter] = {}
        self._lock = threading.Lock()

    # ---- classes ----------------------------------------------------------
    def adapter_class(self, typ: str) -> type[Adapter]:
        if typ not in self.registry:
            raise KeyError(f"unknown channel type {typ!r}")
        mod, cls = self.registry[typ]
        return getattr(importlib.import_module(mod), cls)

    def specs(self) -> dict:
        """{type: (title, SPEC)} for every channel type that can be loaded."""
        out = {}
        for typ in self.registry:
            try:
                k = self.adapter_class(typ)
                out[typ] = (k.title or typ, k.SPEC)
            except Exception:
                log.exception("cannot load channel type %s", typ)
        return out

    # ---- lifecycle --------------------------------------------------------
    def apply(self, typ: str, cfg: dict) -> None:
        """Start a channel with this config, replacing any running one.

        The new adapter is built and started first; the old one is stopped only if that
        worked, so a bad credential never takes down a working channel.
        """
        klass = self.adapter_class(typ)
        problems = klass.validate(cfg)
        if problems:
            raise ValueError("; ".join(problems))
        new = klass(cfg)
        new.start()
        with self._lock:
            old = self._running.get(typ)
            self._running[typ] = new
        if old is not None:
            try:
                old.stop()
            except Exception:
                log.exception("failed to stop previous %s adapter", typ)

    def remove(self, typ: str) -> None:
        with self._lock:
            old = self._running.pop(typ, None)
        if old is not None:
            old.stop()

    def get(self, typ: str) -> Adapter | None:
        return self._running.get(typ)

    def running(self) -> list[str]:
        return sorted(self._running)

    def test(self, typ: str, cfg: dict) -> tuple[bool, str]:
        klass = self.adapter_class(typ)
        problems = klass.validate(cfg)
        if problems:
            return False, "; ".join(problems)
        try:
            return klass(cfg).test()
        except Exception as e:
            return False, str(e)

    def stop_all(self) -> None:
        for typ in list(self._running):
            try:
                self.remove(typ)
            except Exception:
                log.exception("failed to stop %s", typ)

    # ---- webhook routes ---------------------------------------------------
    def mount(self, app) -> None:
        """Mount one route per webhook-capable type. It forwards to whichever adapter is
        running when the request arrives (404 if none), so config changes need no remount."""
        for typ in self.registry:
            try:
                path = self.adapter_class(typ).webhook_path
            except Exception:
                log.exception("cannot load channel type %s", typ)
                continue
            if path:
                app.add_api_route(path, self._dispatcher(typ), methods=["POST"],
                                  include_in_schema=False)

    def _dispatcher(self, typ: str):
        async def route(request: Request):
            adapter = self._running.get(typ)
            if adapter is None:
                raise HTTPException(404)
            return await adapter.handle_webhook(request)
        return route

    # ---- config sources ---------------------------------------------------
    def start_from_store(self, store) -> None:
        """Start every enabled channel saved in the admin database."""
        for typ in store.types():
            found = store.runtime_config(typ)
            if not found:
                continue
            enabled, cfg = found
            if not enabled:
                continue
            try:
                self.apply(typ, cfg)
            except Exception:
                log.exception("failed to start channel %s", typ)

    def start_from_config(self) -> None:
        """Legacy source: channels.yaml. Used only when no master key is configured."""
        cfg = load()
        for typ, ccfg in cfg["channels"].items():
            if not ccfg.get("enabled"):
                continue
            try:
                self.apply(typ, {**cfg["defaults"], **ccfg})
            except Exception:
                log.exception("failed to start channel %s", typ)


manager = ChannelManager()
