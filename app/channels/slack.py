"""Slack adapter using Socket Mode (outbound websocket: no public URL needed)."""
import logging
import re
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from app.channels.base import Adapter, Envelope, Field
from app.channels.core import handle, sources_line, to_mrkdwn
from app.identity import TTLCache, lookup_ok, lookup_problem

log = logging.getLogger(__name__)
_MENTION = re.compile(r"<@[A-Z0-9]+>")


class SlackAdapter(Adapter):
    name = "slack"
    title = "Slack"
    SPEC = (
        Field("bot_token", "Bot token (xoxb-...)", secret=True, required=True,
              help="OAuth & Permissions > Bot User OAuth Token"),
        Field("app_token", "App-level token (xapp-...)", secret=True, required=True,
              help="Basic Information > App-Level Tokens, scope connections:write (Socket Mode)"),
        Field("allowed_channels", "Allowed channels", kind="list",
              help="Channel IDs the bot may answer in. Empty = any channel it is in. DMs always work."),
        Field("kb_ids", "Knowledge bases", kind="kbs",
              help="Which knowledge bases this channel answers from. Empty = the default knowledge base."),
        Field("show_sources", "Show sources", kind="bool", default=True),
    )

    def test(self) -> tuple[bool, str]:
        from slack_sdk import WebClient
        r = WebClient(token=self.cfg["bot_token"]).auth_test()
        return True, f"Connected as {r.get('user')} in workspace {r.get('team')}."

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        if not cfg.get("bot_token") or not cfg.get("app_token"):
            raise RuntimeError("slack: bot_token and app_token are required")
        self.app = App(token=cfg["bot_token"])
        self.handler = SocketModeHandler(self.app, cfg["app_token"])
        self.allowed = set(cfg.get("allowed_channels") or [])
        self.show_sources = cfg.get("show_sources", True)
        self._emails = TTLCache()
        self.app.event("app_mention")(self._on_event)
        self.app.event("message")(self._on_event)

    def _email_for(self, user_id: str) -> str:
        """The sender's email (users.info). Needs the users:read and users:read.email scopes; without
        them senders count as unknown and an alert tells admins what to add."""
        if not user_id:
            return ""
        found, email = self._emails.get(user_id)
        if found:
            return email
        email = ""
        try:
            info = self.app.client.users_info(user=user_id)
            email = ((info.get("user") or {}).get("profile") or {}).get("email", "") or ""
            lookup_ok("identity_lookup:slack")
        except Exception as e:
            err = getattr(getattr(e, "response", None), "data", {}).get("error", "") if hasattr(e, "response") else ""
            log.warning("slack email lookup failed: %s", err or type(e).__name__)
            if err in ("missing_scope", "not_allowed_token_type", "invalid_auth"):
                lookup_problem("identity_lookup:slack", "Slack: cannot read people's email addresses, so everyone is "
                               "treated as an unknown Viewer. Add the users:read and users:read.email scopes to the "
                               "Slack app and re-install it.")
        self._emails.put(user_id, email)
        return email

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
                       event.get("user", ""), text, tuple(self.cfg.get("kb_ids") or ()), self._email_for(event.get("user", "")))
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
