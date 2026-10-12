"""Channel-agnostic contracts. Adapters translate platform events <-> Envelope."""
from abc import ABC
from dataclasses import dataclass


@dataclass(frozen=True)
class Envelope:
    channel: str       # "slack", "teams", "gchat", ...
    workspace: str     # team / tenant / domain id
    room: str          # channel / chat / space id
    thread: str        # thread id (or room id when the platform has no threads)
    user: str
    text: str

    @property
    def conv_id(self) -> str:
        return f"{self.channel}-{self.workspace}-{self.room}-{self.thread}"


class Adapter(ABC):
    """One per platform. Must not contain RAG logic; call channels.handle() instead."""
    name: str

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def router(self):
        """Optional FastAPI APIRouter for webhook-style channels (Teams, Google Chat)."""
        return None

    def start(self) -> None:
        """Start listening (non-blocking). Webhook channels need not override."""

    def stop(self) -> None:
        """Stop listening."""
