"""Sign-in for the Web UI (D2). People use email + password; the server returns a signed token that the
Web UI sends as `Authorization: Bearer <token>`. It is signed with a different key purpose than the
admin cookie, so a chat token can never open the admin panel (and the other way round).

Public routes (rate-limited, no data returned without a password):
    POST /chat-auth/login          {email, password} -> {token, ...}
    POST /chat-auth/set-password   {token, password}  (one-time link from an Admin)
    GET  /chat-auth/me             who the token belongs to
"""
import logging
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.admin import users
from app.admin.auth import LoginLimiter, SessionSigner, fingerprint
from app.config import settings
from app.db import get_db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/chat-auth", tags=["chat"])
_bearer = HTTPBearer(auto_error=False, description="Chat session token from /chat-auth/login")
_by_email, _by_ip = LoginLimiter(5, 300), LoginLimiter(20, 300)
_signer: SessionSigner | None = None
CHAT_TTL = 12 * 3600


@dataclass(frozen=True)
class ChatUser:
    email: str
    name: str
    role: str
    user_id: int

    @property
    def prefix(self) -> str:
        return f"web-{self.user_id}-"


def _get_signer() -> SessionSigner:
    global _signer
    if not settings.secret_key:
        raise HTTPException(503, "Sign-in is unavailable: APP_SECRET_KEY is not set on the server.")
    if _signer is None:
        _signer = SessionSigner(settings.secret_key, ttl=CHAT_TTL, purpose="rag-chat-session-v1")
    return _signer


def web_enabled() -> bool:
    """On unless an Admin has configured the Web UI channel and switched it off."""
    try:
        from app.channels import get_store
        store = get_store()
        view = store.public_view("web") if store else None
        return view is None or bool(view["enabled"])
    except Exception:
        log.warning("could not read the Web UI channel setting; treating it as on", exc_info=True)
        return True


def web_kb_ids() -> list[int]:
    """Knowledge bases the Web UI channel is bound to (empty = the default one)."""
    try:
        from app.channels import get_store
        store = get_store()
        found = store.runtime_config("web") if store else None
        return [int(i) for i in (found[1].get("kb_ids") or [])] if found else []
    except Exception:
        log.warning("could not read the Web UI knowledge bases; using the default", exc_info=True)
        return []


def user_from_token(token: str | None) -> ChatUser | None:
    """The person a valid chat token belongs to, or None (bad, expired, deactivated, password changed)."""
    if not token or "." not in token or not settings.secret_key:
        return None
    payload = _get_signer().read(token)
    if not payload:
        return None
    u = users.get_user(get_db(), payload["sub"])
    if not u or u["status"] != "Active" or not u["password_hash"] or fingerprint(u["password_hash"]) != payload.get("pw"):
        return None
    if not web_enabled():
        return None
    return ChatUser(u["email"], u["name"], u["role"], u["id"])


def bearer_token(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> str | None:
    return credentials.credentials if credentials else None


class LoginBody(BaseModel):
    email: str
    password: str


class SetPasswordBody(BaseModel):
    token: str
    password: str


@router.post("/login")
def login(body: LoginBody, request: Request):
    if not web_enabled():
        raise HTTPException(403, "Web sign-in is turned off.")
    signer = _get_signer()
    email, ip = users.normalize_email(body.email), (request.client.host if request.client else "?")
    if _by_email.blocked(email) or _by_ip.blocked(ip):
        raise HTTPException(429, "Too many failed attempts. Try again in a few minutes.")
    u = users.authenticate_chat(get_db(), email, body.password)
    if u is None:
        _by_email.failure(email)
        _by_ip.failure(ip)
        raise HTTPException(401, "Invalid email or password.")
    _by_email.success(email)
    token, _ = signer.issue(u["email"], fingerprint(u["password_hash"]))
    return {"token": token, "email": u["email"], "name": u["name"], "role": u["role"], "expires_in": CHAT_TTL}


@router.get("/me")
def me(token: str | None = Depends(bearer_token)):
    u = user_from_token(token)
    if u is None:
        raise HTTPException(401, "Sign in required.")
    return {"email": u.email, "name": u.name, "role": u.role}


@router.post("/set-password")
def set_password(body: SetPasswordBody, request: Request):
    ip = request.client.host if request.client else "?"
    if _by_ip.blocked(ip):
        raise HTTPException(429, "Too many attempts. Try again in a few minutes.")
    try:
        u = users.redeem_set_password_link(get_db(), body.token, body.password)
    except ValueError as e:
        _by_ip.failure(ip)
        raise HTTPException(400, str(e))
    try:
        from app.channels import get_store
        store = get_store()
        if store:
            store.record(u["email"], "password.set", u["email"], "via one-time link (Web UI)")
    except Exception:
        log.warning("could not audit password.set", exc_info=True)
    return {"ok": True, "email": u["email"]}
