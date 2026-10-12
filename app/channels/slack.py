"""Slack adapter using Socket Mode (outbound websocket: no public URL needed)."""
import logging
import re
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from app.channels.base import Adapter, Envelope
from app.channels.core import handle, sources_line, to_mrkdwn

log = logging.getLogger(__name__)
_MENTION = re.compile(r"<@[A-Z0-9]+>")


class SlackAdapter(Adapter):
    name = "slack"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        if not cfg.get("bot_token") or not cfg.get("app_token"):
            raise RuntimeError("slack: bot_token and app_token are required")
        self.app = App(token=cfg["bot_token"])
        self.handler = SocketModeHandler(self.app, cfg["app_token"])
        self.allowed = set(cfg.get("allowed_channels") or [])
        self.show_sources = cfg.get("show_sources", True)
        self.app.event("app_mention")(self._on_event)
        self.app.event("message")(self._on_event)

    def _permitted(self, channel: str, is_dm: bool) -> bool:
        return is_dm or not self.allowed or channel in self.allowed

    def _on_event(self, event, say, context):
        if event.get("bot_id") or event.get("subtype"):
            return                                   # ignore bots / edits / joins
        is_dm = event.get("channel_type") == "im"
        # In channels, answer only @mentions (the app_mention event) to avoid double replies.
        if event.get("type") == "message" and not is_dm:
            return
        channel = event["channel"]
        if not self._permitted(channel, is_dm):
            return
        text = _MENTION.sub("", event.get("text", "")).strip()
        if not text:
            return
        thread_ts = event.get("thread_ts") or event["ts"]
        env = Envelope("slack", context.get("team_id") or "t", channel, thread_ts,
                       event.get("user", ""), text)
        result = handle(env)
        out = to_mrkdwn(result["answer"])
        if self.show_sources and result["sources"]:
            out += f"\n\n_Sources: {sources_line(result['sources'])}_"
        say(text=out, thread_ts=thread_ts)

    def start(self) -> None:
        self.handler.connect()                       # non-blocking, own thread
        log.info("slack adapter connected (socket mode)")

    def stop(self) -> None:
        self.handler.close()
