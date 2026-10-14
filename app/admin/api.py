"""Admin API: sign-in, channels (with encrypted credentials), audit log and admin users.

Safety rules enforced here:
  * local only by default: requests that arrive through a proxy or tunnel (forwarded headers)
    or from another machine are refused, because the same server is exposed to Teams and
    Google Chat through a tunnel;
  * cookie session (HttpOnly, SameSite=Strict) plus a CSRF header on every change;
  * secrets are write-only: no response contains a secret value, and error messages are
    scrubbed of them.
"""
import hmac
import logging
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.admin import users
from app.admin.auth import LoginLimiter, SessionSigner, fingerprint
from app.channels import get_store
from app.channels.manager import manager as default_manager
from app.channels.store import ChannelStore
from app.config import settings
from app.db import Database, get_db

log = logging.getLogger(__name__)
COOKIE = "rag_admin"
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded",
                 "cf-connecting-ip", "cf-ray", "true-client-ip", "x-forwarded-proto")

router = APIRouter(prefix="/admin/api", tags=["admin"])


@dataclass
class AdminContext:
    db: Database
    store: ChannelStore
    manager: Any
    signer: SessionSigner
    domain: str = ""
    allow_remote: bool = False
    by_email: LoginLimiter = field(default_factory=lambda: LoginLimiter(5, 300))
    by_ip: LoginLimiter = field(default_factory=lambda: LoginLimiter(20, 300))


_ctx: AdminContext | None = None


def get_ctx() -> AdminContext:
    global _ctx
    if _ctx is None:
        store = get_store()
        if store is None:
            raise HTTPException(503, "Admin is disabled: APP_SECRET_KEY is not set.")
        _ctx = AdminContext(db=get_db(), store=store, manager=default_manager,
                            signer=SessionSigner(settings.secret_key),
                            domain=settings.admin_email_domain,
                            allow_remote=settings.admin_allow_remote)
    return _ctx


# ---- guards ------------------------------------------------------------------
def check_local(request: Request, allow_remote: bool) -> None:
    if allow_remote:
        return
    host = (request.client.host if request.client else "") or ""
    hostname = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    if host not in LOOPBACK or hostname not in LOOPBACK \
            or any(h in request.headers for h in PROXY_HEADERS):
        raise HTTPException(403, "The admin API is only available from this machine.")


def local_only(request: Request, ctx: AdminContext = Depends(get_ctx)) -> None:
    check_local(request, ctx.allow_remote)


@dataclass
class Session:
    email: str
    csrf: str


def admin_session(request: Request, ctx: AdminContext = Depends(get_ctx),
                  _: None = Depends(local_only)) -> Session:
    payload = ctx.signer.read(request.cookies.get(COOKIE))
    user = users.get_admin(ctx.db, payload["sub"]) if payload else None
    if not payload or not user or fingerprint(user["password_hash"]) != payload.get("pw"):
        raise HTTPException(401, "Sign in required.")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        sent = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(sent, payload["csrf"]):
            raise HTTPException(403, "Missing or invalid CSRF token.")
    return Session(email=user["email"], csrf=payload["csrf"])


def _set_cookie(request: Request, response: Response, ctx: AdminContext, user: dict) -> str:
    token, csrf = ctx.signer.issue(user["email"], fingerprint(user["password_hash"]))
    response.set_cookie(COOKIE, token, max_age=ctx.signer.ttl, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", path="/admin")
    return csrf


# ---- auth --------------------------------------------------------------------
class LoginBody(BaseModel):
    email: str
    password: str


class PasswordBody(BaseModel):
    current: str
    new: str


@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response,
          ctx: AdminContext = Depends(get_ctx), _: None = Depends(local_only)):
    email, ip = users.normalize_email(body.email), (request.client.host if request.client else "?")
    if ctx.by_email.blocked(email) or ctx.by_ip.blocked(ip):
        raise HTTPException(429, "Too many failed attempts. Try again in a few minutes.")
    user = users.authenticate(ctx.db, email, body.password)
    if user is None:
        ctx.by_email.failure(email)
        ctx.by_ip.failure(ip)
        ctx.store.record(email[:254] or "unknown", "login.failed", "", "invalid credentials")
        raise HTTPException(401, "Invalid email or password.")
    ctx.by_email.success(email)
    csrf = _set_cookie(request, response, ctx, user)
    ctx.store.record(user["email"], "login")
    return {"email": user["email"], "role": user["role"], "csrf": csrf}


@router.post("/auth/logout")
def logout(response: Response, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    response.delete_cookie(COOKIE, path="/admin")
    ctx.store.record(s.email, "logout")
    return {"ok": True}


@router.get("/auth/me")
def me(s: Session = Depends(admin_session)):
    return {"email": s.email, "csrf": s.csrf}


@router.post("/auth/password")
def change_password(body: PasswordBody, request: Request, response: Response,
                    s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    if users.authenticate(ctx.db, s.email, body.current) is None:
        raise HTTPException(400, "Current password is incorrect.")
    try:
        users.set_password(ctx.db, s.email, body.new)
    except ValueError as e:
        raise HTTPException(422, str(e))
    csrf = _set_cookie(request, response, ctx, users.get_admin(ctx.db, s.email))   # old sessions die
    ctx.store.record(s.email, "password.change", s.email)
    return {"ok": True, "csrf": csrf}


# ---- channels ----------------------------------------------------------------
class ChannelUpdate(BaseModel):
    enabled: bool
    values: dict[str, Any] = {}


class ChannelTest(BaseModel):
    values: dict[str, Any] = {}


def _klass(ctx: AdminContext, typ: str):
    try:
        return ctx.manager.adapter_class(typ)
    except KeyError:
        raise HTTPException(404, f"Unknown channel type {typ!r}.")


def _scrub(message: str, typ: str, ctx: AdminContext, cfg: dict) -> str:
    """Remove any secret value that an error message might echo back."""
    for f in _klass(ctx, typ).SPEC:
        v = cfg.get(f.name)
        if f.secret and isinstance(v, str) and len(v) >= 4:
            message = message.replace(v, "***")
    return message


def _view(ctx: AdminContext, typ: str) -> dict:
    k = _klass(ctx, typ)
    pv = ctx.store.public_view(typ)
    if pv is None:
        fields = [{"name": f.name, "label": f.label, "kind": f.kind, "secret": f.secret,
                   "required": f.required, "help": f.help,
                   **({"is_set": False} if f.secret else {"value": f.default}),
                   **({"choices": list(f.choices)} if f.choices else {})} for f in k.SPEC]
    else:
        choices = {f.name: list(f.choices) for f in k.SPEC if f.choices}
        fields = [{**f, **({"choices": choices[f["name"]]} if f["name"] in choices else {})}
                  for f in pv["fields"]]
    return {"type": typ, "title": k.title or typ, "configured": pv is not None,
            "enabled": bool(pv and pv["enabled"]), "running": ctx.manager.get(typ) is not None,
            "updated_at": pv["updated_at"] if pv else None, "webhook_path": k.webhook_path,
            "fields": fields}


@router.get("/channels")
def list_channels(_: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    return [_view(ctx, t) for t in ctx.manager.registry]


@router.get("/channels/{typ}")
def get_channel(typ: str, _: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    return _view(ctx, typ)


@router.put("/channels/{typ}")
def save_channel(typ: str, body: ChannelUpdate, s: Session = Depends(admin_session),
                 ctx: AdminContext = Depends(get_ctx)):
    """Validate and, if enabled, start the channel BEFORE saving. A failure leaves both the
    stored settings and the working connection untouched."""
    _klass(ctx, typ)
    try:
        merged = ctx.store.merged_config(typ, body.values)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if body.enabled:
        try:
            ctx.manager.apply(typ, merged)
        except ValueError as e:
            raise HTTPException(422, str(e))
        except Exception as e:
            log.warning("channel %s failed to start: %s", typ, type(e).__name__)
            raise HTTPException(400, "Could not start the channel: " + _scrub(str(e), typ, ctx, merged))
    else:
        ctx.manager.remove(typ)
    ctx.store.save(typ, enabled=body.enabled, values=body.values, actor=s.email)
    return _view(ctx, typ)


@router.post("/channels/{typ}/test")
def test_channel(typ: str, body: ChannelTest, s: Session = Depends(admin_session),
                 ctx: AdminContext = Depends(get_ctx)):
    _klass(ctx, typ)
    try:
        merged = ctx.store.merged_config(typ, body.values)
    except ValueError as e:
        raise HTTPException(422, str(e))
    ok, message = ctx.manager.test(typ, merged)
    message = _scrub(message, typ, ctx, merged)
    ctx.store.record(s.email, "channel.test", typ, ("passed: " if ok else "failed: ") + message[:300])
    return {"ok": ok, "message": message}


@router.delete("/channels/{typ}")
def delete_channel(typ: str, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    _klass(ctx, typ)
    ctx.manager.remove(typ)
    if not ctx.store.delete(typ, actor=s.email):
        raise HTTPException(404, "That channel is not configured.")
    return {"ok": True}


# ---- audit & admins ----------------------------------------------------------
@router.get("/audit")
def audit(limit: int = 100, _: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    return ctx.store.recent_audit(max(1, min(limit, 500)))


class NewAdmin(BaseModel):
    email: str
    password: str


@router.get("/admins")
def list_admins(_: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    return users.list_admins(ctx.db)


@router.post("/admins", status_code=201)
def add_admin(body: NewAdmin, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    try:
        created = users.create_admin(ctx.db, body.email, body.password, domain=ctx.domain)
    except ValueError as e:
        raise HTTPException(422, str(e))
    ctx.store.record(s.email, "admin.add", created["email"])
    return users.public(created)


@router.delete("/admins/{email}")
def remove_admin(email: str, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    if users.normalize_email(email) == s.email:
        raise HTTPException(400, "You cannot remove yourself.")
    try:
        removed = users.delete_admin(ctx.db, email)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not removed:
        raise HTTPException(404, "No such admin.")
    ctx.store.record(s.email, "admin.remove", users.normalize_email(email))
    return {"ok": True}
