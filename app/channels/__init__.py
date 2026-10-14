"""Channel adapters. A new platform = one module + one line in manager.REGISTRY."""
import logging

from app.channels.manager import REGISTRY, ChannelManager, manager

__all__ = ["REGISTRY", "ChannelManager", "manager", "mount", "start_all", "stop_all", "get_store"]

log = logging.getLogger(__name__)
_store = None


def get_store():
    """The channel store, or None when no master key is configured."""
    global _store
    if _store is None:
        from app.channels.store import ChannelStore
        from app.config import settings
        from app.db import get_db
        from app.secretstore import FernetSecretStore, SecretError
        try:
            secrets = FernetSecretStore(settings.secret_key)
        except SecretError as e:
            log.warning("%s Falling back to channels.yaml.", e)
            return None
        _store = ChannelStore(get_db(), secrets, lambda t: manager.adapter_class(t).SPEC)
    return _store


def mount(app) -> None:
    manager.mount(app)


def start_all() -> None:
    store = get_store()
    if store is None:
        manager.start_from_config()               # legacy: channels.yaml + .env
        return
    from app.channels.config import load
    imported = store.import_config_once(load())
    if imported:
        log.info("imported channel settings from channels.yaml: %s", ", ".join(imported))
    manager.start_from_store(store)


def stop_all() -> None:
    manager.stop_all()
