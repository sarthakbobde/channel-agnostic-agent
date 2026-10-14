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
from app.channels.base import Adapter, Envelope, Field
from app.channels.core import handle, sources_line, to_mrkdwn

log = logging.getLogger(__name__)
CHAT_ISSUER = "chat@system.gserviceaccount.com"
CHAT_CERTS = f"https://www.googleapis.com/service_accounts/v1/metadata/x509/{CHAT_ISSUER}"
TIMEOUT_S = 25
GREETING = "Hi! Ask me a question about the knowledge base and I'll answer with sources."
TIMEOUT_REPLY = "That took too long. Please try again, or ask a shorter question."


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
        Field("show_sources", "Show sources", kind="bool", default=True),
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
    @staticmethod
    def _parse(body: dict):
        """-> (kind, space, thread, user, text, addon). kind: message|added|ignore"""
        if "chat" in body:                                     # Workspace add-on shape
            chat = body["chat"]
            user = (chat.get("user") or {}).get("name", "")
            if "addedToSpacePayload" in chat:
                return "added", "", "", user, "", True
            p = chat.get("messagePayload")
            if not p:
                return "ignore", "", "", user, "", True
            msg, space = p.get("message", {}), p.get("space", {})
            addon = True
        else:                                                  # classic shape
            kind = body.get("type")
            user = (body.get("user") or {}).get("name", "")
            if kind == "ADDED_TO_SPACE":
                return "added", "", "", user, "", False
            if kind != "MESSAGE":
                return "ignore", "", "", user, "", False
            msg, space = body.get("message", {}), body.get("space", {})
            addon = False
        # argumentText is the message with the @mention already removed
        text = (msg.get("argumentText") or msg.get("text") or "").strip()
        thread = (msg.get("thread") or {}).get("name") or msg.get("name", "")
        return "message", space.get("name", ""), thread, user, text, addon

    @staticmethod
    def _reply(text: str, addon: bool) -> dict:
        if not addon:
            return {"text": text}
        return {"hostAppDataAction": {"chatDataAction": {"createMessageAction": {
            "message": {"text": text}}}}}

    # ---- webhook ----------------------------------------------------------
    async def handle_webhook(self, request: Request):
        try:
            await run_in_threadpool(self._verify, request.headers.get("Authorization", ""))
        except Exception as e:
            log.warning("gchat auth rejected: %s", e)
            raise HTTPException(401)
        body = await request.json()
        kind, space, thread, user, text, addon = self._parse(body)
        if kind == "ignore":
            return {}
        if kind == "added":
            return self._reply(GREETING, addon)
        if not text or (self.allowed and space not in self.allowed):
            return {}
        env = Envelope("gchat", "g", space, thread or space, user, text)
        try:
            result = await asyncio.wait_for(run_in_threadpool(handle, env), TIMEOUT_S)
        except asyncio.TimeoutError:
            return self._reply(TIMEOUT_REPLY, addon)
        out = to_mrkdwn(result["answer"])
        if self.show_sources and result["sources"]:
            out += f"\n\n_Sources: {sources_line(result['sources'])}_"
        return self._reply(out, addon)

    def test(self) -> tuple[bool, str]:
        """Google signs requests; there is no outbound credential to try. Check that the
        signing certificates are reachable so token verification can work."""
        import requests
        r = requests.get(CHAT_CERTS, timeout=10)
        if r.status_code != 200:
            return False, f"Could not fetch Google's signing certificates (HTTP {r.status_code})."
        return True, ("Configuration is valid and Google's signing certificates are reachable. "
                      "Full verification happens when Google sends its first request.")
