"""Microsoft Teams adapter (Azure Bot Service webhook).

Teams POSTs activities to /channels/teams/messages over HTTPS. We acknowledge
immediately, answer in the background, and reply via a proactive message, so slow
LLM calls never hit Teams' request timeout.
"""
import asyncio
import logging
import jwt
from fastapi import HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings, TurnContext
from botbuilder.schema import Activity, ActivityTypes, Attachment
from app.channels.base import Adapter, ChannelPresenter, Envelope, Field
from app.channels.core import handle, handle_action, rating_buttons, sources_footer, suggestion_controls, suggestions_text
from app.identity import TTLCache, lookup_ok, lookup_problem

log = logging.getLogger(__name__)


class TeamsPresenter(ChannelPresenter):
    """Plain text, or an Adaptive Card whose Action.Submit buttons send {caa_action: payload} back as a message value."""

    def render(self, result: dict, show_sources: bool):
        out = result["answer"]
        if show_sources and result["sources"]:
            out += sources_footer(result)
        chips = suggestion_controls(result)
        buttons = [c for c in chips if c["payload"]]
        plain = [c for c in chips if not c["payload"]]
        if plain:
            out += suggestions_text({**result, "suggestions": plain})
        rate = rating_buttons(result)
        if not buttons and not rate:
            return out
        actions = [{"type": "Action.Submit", "title": c["text"][:100], "data": {"caa_action": c["payload"]}} for c in buttons]
        if rate:
            labels = rate["labels"]
            actions += [{"type": "Action.Submit", "title": "\U0001F44D " + labels["up"], "data": {"caa_action": rate["payload"], "caa_rating": "up"}},
                        {"type": "Action.Submit", "title": "\U0001F44E " + labels["down"], "data": {"caa_action": rate["payload"], "caa_rating": "down"}}]
        return self.card(out, actions)

    @staticmethod
    def card(text: str, actions: list, extra: list | None = None) -> Activity:
        body = [{"type": "TextBlock", "text": text, "wrap": True}] + (extra or [])
        card = {"type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "version": "1.4", "body": body, "actions": actions}
        return Activity(type=ActivityTypes.message, attachments=[Attachment(content_type="application/vnd.microsoft.card.adaptive", content=card)])

    def form(self, form: dict) -> Activity:
        """After a thumbs-down: a card with the reason list and a comment box. Teams sends the inputs back with the submit."""
        labels = form["labels"]
        inputs = [{"type": "Input.ChoiceSet", "id": "reason", "style": "compact", "placeholder": labels["reason"],
                   "choices": [{"title": labels["reason_labels"][r], "value": r} for r in form["reasons"]]},
                  {"type": "Input.Text", "id": "comment", "isMultiline": True, "placeholder": labels["comment"]}]
        return self.card(labels["reason"], [{"type": "Action.Submit", "title": labels["submit"],
                                             "data": {"caa_action": form["payload"], "caa_rating": "down"}}], inputs)


class TeamsAdapter(Adapter):
    name = "teams"
    title = "Microsoft Teams"
    webhook_path = "/channels/teams/messages"
    SPEC = (
        Field("app_id", "Microsoft App ID", required=True, help="Azure Bot > Configuration"),
        Field("app_password", "Client secret", secret=True, required=True,
              help="Azure Bot > Configuration > Manage Password"),
        Field("tenant_id", "Tenant ID", help="Required for single-tenant bots"),
        Field("allowed_tenants", "Allowed tenants", kind="list", help="Empty = any tenant"),
        Field("kb_ids", "Knowledge bases", kind="kbs",
              help="Which knowledge bases this channel answers from. Empty = the default knowledge base."),
        Field("show_sources", "Show sources", kind="bool", default=True),
    )

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        if not cfg.get("app_id") or not cfg.get("app_password"):
            raise RuntimeError("teams: app_id and app_password are required")
        self.app_id = cfg["app_id"]
        self.allowed_tenants = set(cfg.get("allowed_tenants") or [])
        self.show_sources = cfg.get("show_sources", True)
        self.bf = BotFrameworkAdapter(BotFrameworkAdapterSettings(
            self.app_id, cfg["app_password"],
            channel_auth_tenant=cfg.get("tenant_id") or None,   # needed for single-tenant bots
        ))
        self._tasks: set[asyncio.Task] = set()
        self.presenter = TeamsPresenter()

    async def handle_webhook(self, request: Request):
        if "application/json" not in request.headers.get("Content-Type", ""):
            raise HTTPException(415)
        activity = Activity().deserialize(await request.json())
        auth = request.headers.get("Authorization", "")
        try:
            await self.bf.process_activity(activity, auth, self._on_turn)
        except (PermissionError, jwt.PyJWTError):
            raise HTTPException(401)
        return Response(status_code=202)

    def test(self) -> tuple[bool, str]:
        from botframework.connector.auth import MicrosoftAppCredentials
        creds = MicrosoftAppCredentials(self.app_id, self.cfg["app_password"],
                                        channel_auth_tenant=self.cfg.get("tenant_id") or None)
        if not creds.get_access_token():
            return False, "Microsoft did not return a token for these credentials."
        return True, "Microsoft accepted the app ID and secret."

    async def _email_for(self, ctx: TurnContext, user_id: str) -> str:
        """The sender's email from the Teams roster (no Microsoft Graph needed). Unverified on a real tenant."""
        if not user_id:
            return ""
        if not hasattr(self, "_emails"):
            self._emails = TTLCache()
        found, email = self._emails.get(user_id)
        if found:
            return email
        email = ""
        try:
            from botbuilder.core.teams import TeamsInfo
            member = await TeamsInfo.get_member(ctx, user_id)
            email = (getattr(member, "email", "") or getattr(member, "user_principal_name", "") or "")
            lookup_ok("identity_lookup:teams")
        except Exception as e:
            log.warning("teams email lookup failed: %s", type(e).__name__)
            lookup_problem("identity_lookup:teams", "Teams: cannot read people's email addresses, so everyone is "
                           "treated as an unknown Viewer. Check the bot is installed for the user (personal or team scope).")
        self._emails.put(user_id, email)
        return email

    async def _on_turn(self, ctx: TurnContext) -> None:
        act = ctx.activity
        if act.type != ActivityTypes.message:
            return
        payload = act.value.get("caa_action") if isinstance(act.value, dict) else None    # an Action.Submit: a value, no text
        fields = {k: (act.value.get(a) or "") for k, a in (("rating", "caa_rating"), ("reason", "reason"), ("comment", "comment"))} if payload else {}
        if not act.text and not payload:
            return
        if act.text:
            TurnContext.remove_recipient_mention(act)
        text = (act.text or "").strip()
        if not text and not payload:
            return
        tenant = (act.channel_data or {}).get("tenant", {}).get("id", "") or "t"
        if self.allowed_tenants and tenant not in self.allowed_tenants:
            return
        sender = act.from_property.id if act.from_property else ""
        env = Envelope("teams", tenant, act.conversation.id, act.conversation.id, sender, text,
                       tuple(self.cfg.get("kb_ids") or ()), await self._email_for(ctx, sender))
        await ctx.send_activity(Activity(type=ActivityTypes.typing))
        ref = TurnContext.get_conversation_reference(act)
        task = asyncio.create_task(self._answer(ref, env, payload, fields))     # ack now, answer later
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer(self, ref, env: Envelope, payload: str | None = None, fields: dict | None = None) -> None:
        result = (await run_in_threadpool(lambda: handle_action(env, payload, **{k: v for k, v in (fields or {}).items() if v})) if payload else await run_in_threadpool(handle, env))
        if result.get("ignored"):
            return
        out = self.presenter.render(result, self.show_sources)
        form = result.get("feedback_form")

        async def send(ctx: TurnContext):
            await ctx.send_activity(out)
            if form:
                await ctx.send_activity(self.presenter.form(form))

        await self.bf.continue_conversation(ref, send, self.app_id)
