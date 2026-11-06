"""Google Chat adapter (HTTP endpoint app).

Google POSTs events to /channels/gchat/events over HTTPS and expects the reply in the
HTTP response within 30 seconds, so the answer is returned synchronously (guarded by a
timeout). Both event shapes are supported:
  * classic Chat app:   {"type": "MESSAGE", "message": {...}, "space": {...}}
  * Workspace add-on:   {"chat": {"messagePayload": {"message": {...}, "space": {...}}}}
Every request carries a Google-signed bearer token, verified per `auth` mode:
  * auth: project_number -> token issued by chat@system.gserviceaccount.com,
                            audience = your Google Cloud project NUMBER
  * auth: endpoint_url   -> token issued to the app's service account,
                            audience = the endpoint URL, email must equal service_account_email
"""
import asyncio
import logging
from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from google.auth.transport import requests as g_requests
from google.oauth2 import id_token
from app.channels.base import Adapter, ChannelPresenter, Envelope, Field
from app.channels.core import handle, handle_action, rating_buttons, sources_footer, suggestion_controls, suggestions_text, to_mrkdwn

log = logging.getLogger(__name__)
CHAT_ISSUER = "chat@system.gserviceaccount.com"
CHAT_CERTS = f"https://www.googleapis.com/service_accounts/v1/metadata/x509/{CHAT_ISSUER}"
TIMEOUT_S = 25
GREETING = "Hi! Ask me a question about the knowledge base and I'll answer with sources."
TIMEOUT_REPLY = "That took too long. Please try again, or ask a shorter question."


FUNCTION = "caa_suggest"
RATE = "caa_rate"              # a thumbs-up or thumbs-down
RATE_FORM = "caa_rate_form"    # the reason and comment after a thumbs-down
FUNCTIONS = (FUNCTION, RATE, RATE_FORM)


class GChatPresenter(ChannelPresenter):
    """Text, plus a cardsV2 card of buttons in the same synchronous reply (D30). With buttons off, suggestions are text."""

    def __init__(self, buttons: bool = True):
        self.buttons = buttons

    def render(self, result: dict, show_sources: bool) -> tuple[str, list]:
        out = to_mrkdwn(result["answer"])
        if show_sources and result["sources"]:
            out += sources_footer(result)
        chips = suggestion_controls(result)
        buttons = [c for c in chips if c["payload"]] if self.buttons else []
        plain = [c for c in chips if c not in buttons]
        if plain:
            out += suggestions_text({**result, "suggestions": plain})
        cards = [{"cardId": "caa-suggestions", "card": {"sections": [{"widgets": [{"buttonList": {"buttons": [
            {"text": c["text"][:100], "onClick": {"action": {"function": FUNCTION, "parameters": [{"key": "payload", "value": c["payload"]}]}}}
            for c in buttons]}}]}]}}] if buttons else []
        rate = rating_buttons(result) if self.buttons else None        # with buttons off there is no rating either: it needs the card
        if rate:
            labels = rate["labels"]
            cards.append({"cardId": "caa-rating", "card": {"sections": [{"header": labels["prompt"], "widgets": [{"buttonList": {"buttons": [
                {"text": "\U0001F44D " + labels["up"], "onClick": {"action": {"function": RATE, "parameters": [
                    {"key": "payload", "value": rate["payload"]}, {"key": "rating", "value": "up"}]}}},
                {"text": "\U0001F44E " + labels["down"], "onClick": {"action": {"function": RATE, "parameters": [
                    {"key": "payload", "value": rate["payload"]}, {"key": "rating", "value": "down"}]}}}]}}]}]}})
        form = result.get("feedback_form") if self.buttons else None
        if form:
            cards.append(self.form_card(form))
        return out, cards

    @staticmethod
    def form_card(form: dict) -> dict:
        """After a thumbs-down: a card with a reason dropdown and a comment box. Google sends the inputs back with the button click."""
        labels = form["labels"]
        return {"cardId": "caa-rating-form", "card": {"sections": [{"header": labels["reason"], "widgets": [
            {"selectionInput": {"name": "reason", "label": labels["reason"], "type": "DROPDOWN",
                                "items": [{"text": labels["reason_labels"][r], "value": r, "selected": False} for r in form["reasons"]]}},
            {"textInput": {"name": "comment", "label": labels["comment"], "type": "MULTIPLE_LINE"}},
            {"textInput": {"name": "link", "label": labels["link"], "type": "SINGLE_LINE"}},
            {"buttonList": {"buttons": [{"text": labels["submit"], "onClick": {"action": {"function": RATE_FORM, "parameters": [
                {"key": "payload", "value": form["payload"]}, {"key": "rating", "value": "down"}]}}}]}}]}]}}


class GChatAdapter(Adapter):
    name = "gchat"
    title = "Google Chat"
    webhook_path = "/channels/gchat/events"
    SPEC = (
        Field("auth", "Authentication audience", kind="choice", default="project_number",
              choices=("project_number", "endpoint_url"),
              help="Must match 'Authentication Audience' in the Chat API configuration"),
        Field("audience", "Audience", required=True,
              help="Google Cloud project NUMBER, or the endpoint URL when auth=endpoint_url"),
        Field("service_account_email", "Service account email",
              help="Only for auth=endpoint_url"),
        Field("allowed_spaces", "Allowed spaces", kind="list", help="e.g. spaces/AAAA... Empty = any"),
        Field("kb_ids", "Knowledge bases", kind="kbs",
              help="Which knowledge bases this channel answers from. Empty = the default knowledge base."),
        Field("show_sources", "Show sources", kind="bool", default=True),
        Field("suggestion_buttons", "Buttons on cards", kind="bool", default=True,
              help="Off shows suggested questions as text and hides the rating buttons, for spaces where card buttons do not work."),
    )

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.auth = cfg.get("auth", "project_number")
        self.audience = cfg.get("audience")
        self.sa_email = cfg.get("service_account_email")
        if self.auth not in ("project_number", "endpoint_url"):
            raise RuntimeError("gchat: auth must be project_number or endpoint_url")
        if not self.audience:
            raise RuntimeError("gchat: audience is required")
        if self.auth == "endpoint_url" and not self.sa_email:
            raise RuntimeError("gchat: service_account_email is required for auth=endpoint_url")
        self.allowed = set(cfg.get("allowed_spaces") or [])
        self.show_sources = cfg.get("show_sources", True)
        self.presenter = GChatPresenter(cfg.get("suggestion_buttons", True))

    # ---- auth -------------------------------------------------------------
    def _verify(self, authorization: str) -> None:
        """Raise on any invalid token. Blocking (fetches Google certs): run in a thread."""
        if not authorization.lower().startswith("bearer "):
            raise PermissionError("missing bearer token")
        token = authorization.split(" ", 1)[1].strip()
        req = g_requests.Request()
        if self.auth == "project_number":
            claims = id_token.verify_token(token, req, audience=str(self.audience),
                                           certs_url=CHAT_CERTS)
            if claims.get("iss") != CHAT_ISSUER:
                raise PermissionError("unexpected issuer")
        else:
            claims = id_token.verify_oauth2_token(token, req, audience=self.audience)
            if claims.get("email") != self.sa_email or not claims.get("email_verified"):
                raise PermissionError("unexpected service account")

    # ---- event parsing / response -----------------------------------------
    @classmethod
    def click_payload(cls, body: dict) -> str | None:
        """The signed payload of a suggestion button click, in either event format; None for anything else."""
        return cls._click(body)[1].get("payload")

    @staticmethod
    def _click(body: dict) -> tuple[str | None, dict]:
        """(function, parameters) of a button click in either event format; (None, {}) for anything else."""
        common = body.get("commonEventObject") or {}
        if common.get("invokedFunction") in FUNCTIONS:                        # add-on shape
            return common["invokedFunction"], dict(common.get("parameters") or {})
        action = body.get("action") or {}
        if body.get("type") == "CARD_CLICKED" and action.get("actionMethodName") in FUNCTIONS:   # classic shape
            return action["actionMethodName"], {p.get("key"): p.get("value") for p in action.get("parameters") or []}
        return None, {}

    @classmethod
    def click_fields(cls, body: dict) -> dict:
        """What a rating click carries: the rating, and for the form the reason and comment typed into it."""
        function, params = cls._click(body)
        if function not in (RATE, RATE_FORM):
            return {}
        out = {"rating": params.get("rating", "")}
        inputs = ((body.get("commonEventObject") or {}).get("formInputs") or (body.get("common") or {}).get("formInputs") or {}) if function == RATE_FORM else {}
        for name in ("reason", "comment", "link"):
            values = ((inputs.get(name) or {}).get("stringInputs") or {}).get("value") or []
            out[name] = values[0] if values else ""
        link = out.pop("link")
        if link:
            out["attachments"] = [link]
        return out

    @classmethod
    def _parse(cls, body: dict):
        """-> (kind, space, thread, user, text, addon). kind: message|click|added|ignore"""
        click = cls.click_payload(body) is not None
        if "chat" in body:                                     # Workspace add-on shape
            chat = body["chat"]
            user = (chat.get("user") or {}).get("name", "")
            if "addedToSpacePayload" in chat:
                return "added", "", "", user, "", True
            p = chat.get("buttonClickedPayload") if click else chat.get("messagePayload")
            if not p:
                return "ignore", "", "", user, "", True
            msg, space = p.get("message", {}), p.get("space", {})
            addon = True
        else:                                                  # classic shape
            kind = body.get("type")
            user = (body.get("user") or {}).get("name", "")
            if kind == "ADDED_TO_SPACE":
                return "added", "", "", user, "", False
            if kind != "MESSAGE" and not click:
                return "ignore", "", "", user, "", False
            msg, space = body.get("message", {}), body.get("space", {})
            addon = False
        if click:
            thread = (msg.get("thread") or {}).get("name") or msg.get("name", "")
            return "click", space.get("name", ""), thread, user, "", addon
        # argumentText is the message with the @mention already removed
        text = (msg.get("argumentText") or msg.get("text") or "").strip()
        thread = (msg.get("thread") or {}).get("name") or msg.get("name", "")
        return "message", space.get("name", ""), thread, user, text, addon

    @staticmethod
    def _reply(text: str, addon: bool, cards: list | None = None, click: bool = False) -> dict:
        message = {"text": text, **({"cardsV2": cards} if cards else {})}
        if not addon:
            return {**message, "actionResponse": {"type": "NEW_MESSAGE"}} if click else message
        return {"hostAppDataAction": {"chatDataAction": {"createMessageAction": {"message": message}}}}

    # ---- webhook ----------------------------------------------------------
    async def handle_webhook(self, request: Request):
        try:
            await run_in_threadpool(self._verify, request.headers.get("Authorization", ""))
        except Exception as e:
            log.warning("gchat auth rejected: %s", e)
            raise HTTPException(401)
        body = await request.json()
        kind, space, thread, user, text, addon = self._parse(body)
        sender = (body.get("chat") or body).get("user") or {}
        if kind == "ignore":
            return {}
        if kind == "added":
            return self._reply(GREETING, addon)
        if (kind == "message" and not text) or (self.allowed and space not in self.allowed):
            return {}
        env = Envelope("gchat", "g", space, thread or space, user, text, tuple(self.cfg.get("kb_ids") or ()), sender.get("email", ""))
        try:
            if kind == "click":
                result = await asyncio.wait_for(run_in_threadpool(lambda: handle_action(env, self.click_payload(body), **self.click_fields(body))), TIMEOUT_S)
                if result.get("ignored"):
                    return {}
            else:
                result = await asyncio.wait_for(run_in_threadpool(handle, env), TIMEOUT_S)
        except asyncio.TimeoutError:
            from app import catalog, chat_service
            return self._reply(catalog.t("gchat_timeout", chat_service.reply_language(text, env.conv_id)), addon)
        out, cards = self.presenter.render(result, self.show_sources)
        return self._reply(out, addon, cards, click=kind == "click")

    def test(self) -> tuple[bool, str]:
        """Google signs requests; there is no outbound credential to try. Check that the
        signing certificates are reachable so token verification can work."""
        import requests
        r = requests.get(CHAT_CERTS, timeout=10)
        if r.status_code != 200:
            return False, f"Could not fetch Google's signing certificates (HTTP {r.status_code})."
        return True, ("Configuration is valid and Google's signing certificates are reachable. "
                      "Full verification happens when Google sends its first request.")
