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


@dataclass(frozen=True)
class Field:
    """One setting of an adapter. The admin UI renders its form from these."""
    name: str
    label: str
    kind: str = "text"            # text | list | bool | choice
    secret: bool = False          # stored encrypted, never shown again
    required: bool = False
    help: str = ""
    choices: tuple = ()           # for kind == "choice"
    default: object = None


class Adapter(ABC):
    """One per platform. Must not contain RAG logic; call channels.core.handle() instead.

    Socket-style platforms override start()/stop(). Webhook platforms set
    `webhook_path` and implement handle_webhook(); the manager mounts the route once
    and forwards to whichever adapter instance is currently running, so settings can
    change without a restart.
    """
    name: str
    title: str = ""
    SPEC: tuple[Field, ...] = ()
    webhook_path: str | None = None

    def __init__(self, cfg: dict):
        self.cfg = cfg

    @classmethod
    def validate(cls, cfg: dict) -> list[str]:
        """Human-readable problems with a config, empty if it looks complete."""
        errors = []
        for f in cls.SPEC:
            v = cfg.get(f.name)
            if f.required and (v is None or v == "" or v == []):
                errors.append(f"{f.label} is required")
            if f.kind == "choice" and v not in (None, "") and f.choices and v not in f.choices:
                errors.append(f"{f.label} must be one of: {', '.join(f.choices)}")
        return errors

    def start(self) -> None:
        """Start listening (non-blocking). Webhook channels need not override."""

    def stop(self) -> None:
        """Stop listening."""

    async def handle_webhook(self, request):
        """Handle an inbound HTTP request (webhook channels only)."""
        raise NotImplementedError

    def test(self) -> tuple[bool, str]:
        """Check the credentials against the platform. Returns (ok, message)."""
        return True, "No connection test available for this channel."
