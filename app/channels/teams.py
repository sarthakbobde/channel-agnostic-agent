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
from botbuilder.schema import Activity, ActivityTypes
from app.channels.base import Adapter, Envelope, Field
from app.channels.core import handle, sources_line
from app.identity import TTLCache, lookup_ok, lookup_problem

log = logging.getLogger(__name__)


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
        if act.type != ActivityTypes.message or not act.text:
            return
        TurnContext.remove_recipient_mention(act)
        text = (act.text or "").strip()
        if not text:
            return
        tenant = (act.channel_data or {}).get("tenant", {}).get("id", "") or "t"
        if self.allowed_tenants and tenant not in self.allowed_tenants:
            return
        sender = act.from_property.id if act.from_property else ""
        env = Envelope("teams", tenant, act.conversation.id, act.conversation.id, sender, text,
                       tuple(self.cfg.get("kb_ids") or ()), await self._email_for(ctx, sender))
        await ctx.send_activity(Activity(type=ActivityTypes.typing))
        ref = TurnContext.get_conversation_reference(act)
        task = asyncio.create_task(self._answer(ref, env))     # ack now, answer later
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer(self, ref, env: Envelope) -> None:
        result = await run_in_threadpool(handle, env)
        out = result["answer"]
        if self.show_sources and result["sources"]:
            out += f"\n\n_Sources: {sources_line(result['sources'])}_"

        async def send(ctx: TurnContext):
            await ctx.send_activity(out)

        await self.bf.continue_conversation(ref, send, self.app_id)
