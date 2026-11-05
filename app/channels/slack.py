"""Slack adapter using Socket Mode (outbound websocket: no public URL needed)."""
import json
import logging
import re
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from app.channels.base import Adapter, ChannelPresenter, Envelope, Field
from app.channels.core import handle, handle_action, rating_buttons, sources_footer, suggestion_controls, suggestions_text, to_mrkdwn
from app.identity import TTLCache, lookup_ok, lookup_problem

log = logging.getLogger(__name__)
_MENTION = re.compile(r"<@[A-Z0-9]+>")
ACTION_ID = re.compile(r"^caa_suggest_\d+$")
RATE_ID = re.compile(r"^caa_rate_(up|down)$")
RATE_FORM = "caa_rate_form"


class SlackPresenter(ChannelPresenter):
    """Text plus Block Kit buttons (Interactivity must be on in the Slack app; Socket Mode needs no URL)."""

    def render(self, result: dict, show_sources: bool) -> dict:
        out = to_mrkdwn(result["answer"])
        if show_sources and result["sources"]:
            out += sources_footer(result)
        chips = suggestion_controls(result)
        buttons = [c for c in chips if c["payload"]]
        plain = [c for c in chips if not c["payload"]]
        if plain:
            out += suggestions_text({**result, "suggestions": plain})
        rate = rating_buttons(result)
        if not buttons and not rate:
            return {"text": out}
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": out[:3000]}}]
        if buttons:
            blocks.append({"type": "actions", "elements": [{"type": "button", "action_id": f"caa_suggest_{i}", "value": c["payload"],
                                                            "text": {"type": "plain_text", "text": c["text"][:75], "emoji": False}}
                                                           for i, c in enumerate(buttons)]})
        if rate:
            blocks.append(self.rating_block(rate))
        return {"text": out, "blocks": blocks}

    @staticmethod
    def rating_block(rate: dict) -> dict:
        """Thumbs up and down under the answer. A thumbs-down also opens a form for the reason and comment (modal)."""
        labels = rate["labels"]
        return {"type": "actions", "block_id": "caa_rating", "elements": [
            {"type": "button", "action_id": "caa_rate_up", "value": rate["payload"],
             "text": {"type": "plain_text", "text": ("\U0001F44D " + labels["up"])[:75], "emoji": True}},
            {"type": "button", "action_id": "caa_rate_down", "value": rate["payload"],
             "text": {"type": "plain_text", "text": ("\U0001F44E " + labels["down"])[:75], "emoji": True}}]}

    @staticmethod
    def form_view(form: dict, meta: dict) -> dict:
        """The modal that asks for the reason and an optional comment."""
        labels = form["labels"]
        options = [{"text": {"type": "plain_text", "text": labels["reason_labels"][r][:75]}, "value": r} for r in form["reasons"]]
        return {"type": "modal", "callback_id": RATE_FORM, "private_metadata": json.dumps(meta),
                "title": {"type": "plain_text", "text": labels["down"][:24]}, "submit": {"type": "plain_text", "text": labels["submit"][:24]},
                "close": {"type": "plain_text", "text": "x"},
                "blocks": [
                    {"type": "input", "block_id": "reason", "optional": True, "label": {"type": "plain_text", "text": labels["reason"][:200]},
                     "element": {"type": "static_select", "action_id": "reason", "options": options}},
                    {"type": "input", "block_id": "comment", "optional": True, "label": {"type": "plain_text", "text": labels["comment"][:200]},
                     "element": {"type": "plain_text_input", "action_id": "comment", "multiline": True}}]}


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
        self.app.action(ACTION_ID)(self._on_action)
        self.app.action(RATE_ID)(self._on_rate)
        self.app.view(RATE_FORM)(self._on_rate_form)
        self.presenter = SlackPresenter()

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

    def healthy(self) -> bool:
        try:
            return bool(self.handler.client.is_connected())
        except Exception:
            return False

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
        say(**self.presenter.render(result, self.show_sources), thread_ts=thread_ts)

    def _on_action(self, ack, body, say):
        """A suggestion button: the reply goes to the same thread, as an ordinary question there."""
        ack()
        action = (body.get("actions") or [{}])[0]
        user = (body.get("user") or {}).get("id", "")
        channel = (body.get("channel") or {}).get("id", "")
        message = body.get("message") or {}
        thread_ts = message.get("thread_ts") or message.get("ts", "")
        team = (body.get("team") or {}).get("id") or (body.get("user") or {}).get("team_id") or "t"
        if not channel or not self._permitted(channel, channel.startswith("D")):
            return
        env = Envelope("slack", team, channel, thread_ts, user, "", tuple(self.cfg.get("kb_ids") or ()), self._email_for(user))
        result = handle_action(env, action.get("value", ""))
        if not result.get("ignored"):
            say(**self.presenter.render(result, self.show_sources), thread_ts=thread_ts)

    def _rate_env(self, channel: str, thread_ts: str, team: str, user: str) -> Envelope:
        return Envelope("slack", team, channel, thread_ts, user, "", tuple(self.cfg.get("kb_ids") or ()), self._email_for(user))

    def _on_rate(self, ack, body, client):
        """A thumbs-up or thumbs-down. The rating is stored at once; a thumbs-down also opens the form for a reason and comment."""
        ack()
        action = (body.get("actions") or [{}])[0]
        rating = "down" if action.get("action_id") == "caa_rate_down" else "up"
        user = (body.get("user") or {}).get("id", "")
        channel = (body.get("channel") or {}).get("id", "")
        message = body.get("message") or {}
        thread_ts = message.get("thread_ts") or message.get("ts", "")
        team = (body.get("team") or {}).get("id") or (body.get("user") or {}).get("team_id") or "t"
        if not channel or not self._permitted(channel, channel.startswith("D")):
            return
        result = handle_action(self._rate_env(channel, thread_ts, team, user), action.get("value", ""), rating=rating)
        form = result.get("feedback_form")
        if form and body.get("trigger_id"):
            meta = {"p": action.get("value", ""), "c": channel, "t": thread_ts, "w": team}
            try:
                client.views_open(trigger_id=body["trigger_id"], view=self.presenter.form_view(form, meta))
                return
            except Exception:
                log.warning("slack: could not open the feedback form", exc_info=True)
        self._acknowledge(client, channel, user, thread_ts, result)

    def _on_rate_form(self, ack, body, view, client):
        """The reason and comment from the form (an optional follow-up to the thumbs-down)."""
        ack()
        meta = json.loads(view.get("private_metadata") or "{}")
        values = view.get("state", {}).get("values", {})
        reason = (((values.get("reason") or {}).get("reason") or {}).get("selected_option") or {}).get("value", "")
        comment = ((values.get("comment") or {}).get("comment") or {}).get("value") or ""
        user = (body.get("user") or {}).get("id", "")
        if not meta.get("c") or not (reason or comment):
            return
        result = handle_action(self._rate_env(meta["c"], meta.get("t", ""), meta.get("w") or "t", user), meta.get("p", ""),
                               rating="down", reason=reason, comment=comment)
        self._acknowledge(client, meta["c"], user, meta.get("t", ""), result)

    @staticmethod
    def _acknowledge(client, channel: str, user: str, thread_ts: str, result: dict) -> None:
        """The thanks goes only to the person who rated (an ephemeral message), so the thread stays quiet."""
        if not result.get("answer"):
            return
        try:
            client.chat_postEphemeral(channel=channel, user=user, text=result["answer"], **({"thread_ts": thread_ts} if thread_ts else {}))
        except Exception:
            log.warning("slack: could not acknowledge the rating", exc_info=True)

    def start(self) -> None:
        self.handler.connect()                       # non-blocking, own thread
        log.info("slack adapter connected (socket mode)")

    def stop(self) -> None:
        self.handler.close()
