"""Microsoft Teams adapter (Azure Bot Service webhook).

Teams POSTs activities to /channels/teams/messages over HTTPS. We acknowledge
immediately, answer in the background, and reply via a proactive message, so slow
LLM calls never hit Teams' request timeout.
"""
import asyncio
import logging
import jwt
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings, TurnContext
from botbuilder.schema import Activity, ActivityTypes
from app.channels.base import Adapter, Envelope
from app.channels.core import handle, sources_line

log = logging.getLogger(__name__)


class TeamsAdapter(Adapter):
    name = "teams"

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

    def router(self) -> APIRouter:
        r = APIRouter()

        @r.post("/channels/teams/messages")
        async def messages(request: Request):
            if "application/json" not in request.headers.get("Content-Type", ""):
                raise HTTPException(415)
            activity = Activity().deserialize(await request.json())
            auth = request.headers.get("Authorization", "")
            try:
                await self.bf.process_activity(activity, auth, self._on_turn)
            except (PermissionError, jwt.PyJWTError):
                raise HTTPException(401)
            return Response(status_code=202)

        return r

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
        env = Envelope("teams", tenant, act.conversation.id, act.conversation.id,
                       act.from_property.id if act.from_property else "", text)
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
