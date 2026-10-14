"""Channel adapters. A new platform = one module + one line in manager.REGISTRY."""
from app.channels.manager import REGISTRY, ChannelManager, manager

__all__ = ["REGISTRY", "ChannelManager", "manager", "mount", "start_all", "stop_all"]


def mount(app) -> None:
    manager.mount(app)


def start_all() -> None:
    manager.start_from_config()


def stop_all() -> None:
    manager.stop_all()
