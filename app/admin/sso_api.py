"""Sign in with Microsoft or Google (see sso.py), and its settings screen.

Public (no session; local-only like the rest of the panel unless remote admin is on):
    GET /admin/api/auth/sso                       which buttons the sign-in page shows
    GET /admin/api/auth/sso/{provider}/start      sends the browser to Microsoft or Google
    GET /admin/api/auth/sso/{provider}/callback   where they send it back; signs the person in
Admin only (default-deny in roles.py):
    GET/PUT/DELETE /admin/api/sso[/{provider}]    the settings, and the return address to register

The callback always ends with a redirect to /admin: on success with the session cookie set, otherwise with
?sso_error=<code>, which the page turns into a short message. Details go to the audit log and server log.
"""
import hmac
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.admin import sso, users
from app.admin.api import AdminContext, Session, _set_cookie, admin_session, get_ctx, is_https, local_only
from app.config import settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/api", tags=["admin"])
STATE_COOKIE = "rag_sso"
STATE_PATH = "/admin/api/auth/sso"
STATE_TTL = 600                    # seconds the person has to finish signing in with the provider


def _provider(provider: str) -> str:
    if provider not in sso.PROVIDERS:
        raise HTTPException(404, "Unknown sign-in provider.")
    return provider


def _state_signer(ctx: AdminContext):
    return ctx.signer.child("rag-sso-state-v1", STATE_TTL)


def redirect_uri(request: Request, provider: str) -> str:
    """The return address to register with the provider. ADMIN_PUBLIC_URL wins; otherwise this request's own."""
    base = settings.admin_public_url.rstrip("/")
    if not base:
        url = request.base_url
        base = str(url.replace(scheme="https") if is_https(request) else url).rstrip("/")
    return f"{base}{STATE_PATH}/{provider}/callback"


def _back(error: str = "") -> RedirectResponse:
    r = RedirectResponse("/admin" + (f"?sso_error={error}" if error else ""), status_code=303)
    r.delete_cookie(STATE_COOKIE, path=STATE_PATH)
    return r


# ---- public: sign-in -----------------------------------------------------------------------------------
@router.get("/auth/sso")
def sign_in_options(ctx: AdminContext = Depends(get_ctx), _: None = Depends(local_only)):
    return {"providers": sso.enabled_providers(ctx.db, ctx.store.secrets)}


@router.get("/auth/sso/{provider}/start")
def start(provider: str, request: Request, ctx: AdminContext = Depends(get_ctx), _: None = Depends(local_only)):
    cfg = sso.load(ctx.db, ctx.store.secrets, _provider(provider))
    if not cfg.ready:
        return _back("unavailable")
    try:
        url, keep = sso.begin(cfg, redirect_uri(request, provider))
    except sso.SsoError as e:
        log.warning("sign-in with %s could not start: %s", provider, e.detail)
        return _back(e.code)
    r = RedirectResponse(url, status_code=302)
    # Lax, not Strict: the provider sends the browser back with a cross-site navigation, which must carry it.
    r.set_cookie(STATE_COOKIE, _state_signer(ctx).sign(keep), max_age=STATE_TTL, httponly=True, samesite="lax",
                 secure=is_https(request), path=STATE_PATH)
    return r


@router.get("/auth/sso/{provider}/callback")
def callback(provider: str, request: Request, ctx: AdminContext = Depends(get_ctx), _: None = Depends(local_only)):
    _provider(provider)
    ip = request.client.host if request.client else "?"
    if ctx.by_ip.blocked(ip):
        return _back("blocked")
    q = request.query_params
    keep = _state_signer(ctx).read(request.cookies.get(STATE_COOKIE))
    if not keep or keep.get("p") != provider or not hmac.compare_digest(str(keep.get("s", "")), q.get("state", "")):
        ctx.by_ip.failure(ip)
        ctx.store.record("unknown", "login.sso_failed", provider, "sign-in expired or did not start here")
        return _back("expired")
    if q.get("error"):
        cancelled = q["error"] in ("access_denied", "consent_required", "interaction_required")
        if not cancelled:
            ctx.store.record("unknown", "login.sso_failed", provider, f"provider error: {q['error']}"[:200])
        return _back("cancelled" if cancelled else "failed")
    cfg = sso.load(ctx.db, ctx.store.secrets, provider)
    if not cfg.ready or not q.get("code"):
        return _back("unavailable" if not cfg.ready else "failed")
    try:
        subject, email = sso.finish(cfg, keep, q["code"])
    except sso.SsoError as e:
        ctx.by_ip.failure(ip)
        log.warning("sign-in with %s failed: %s", provider, e.detail)
        ctx.store.record("unknown", "login.sso_failed", provider, e.detail[:200])
        return _back(e.code)
    user, outcome = users.sso_sign_in(ctx.db, provider, subject, email)
    if user is None:
        ctx.by_ip.failure(ip)
        ctx.store.record(email[:254], "login.sso_denied", provider, outcome)
        return _back(outcome)
    ctx.by_ip.success(ip)
    r = _back()
    _set_cookie(request, r, ctx, user)
    users.touch_sign_in(ctx.db, user["email"])
    if outcome == "linked":
        ctx.store.record(user["email"], "user.sso_link", user["email"], f"{sso.PROVIDERS[provider]['label']} account linked on first sign-in")
    ctx.store.record(user["email"], "login", "", f"with {sso.PROVIDERS[provider]['label']}")
    return r


# ---- admin: settings ------------------------------------------------------------------------------------
class ProviderBody(BaseModel):
    enabled: bool = False
    client_id: str = ""
    client_secret: str | None = None
    extra: str = ""


@router.get("/sso")
def settings_view(request: Request, _: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    out = []
    for p in sso.PROVIDERS:
        view = sso.public_view(ctx.db, ctx.store.secrets, p)
        view["redirect_uri"] = redirect_uri(request, p)
        out.append(view)
    return {"providers": out, "domain": ctx.domain}


@router.put("/sso/{provider}")
def settings_save(provider: str, body: ProviderBody, request: Request, s: Session = Depends(admin_session),
                  ctx: AdminContext = Depends(get_ctx)):
    try:
        changed = sso.save(ctx.db, ctx.store.secrets, _provider(provider), enabled=body.enabled,
                           client_id=body.client_id, client_secret=body.client_secret, extra=body.extra)
    except ValueError as e:
        raise HTTPException(422, str(e))
    sso.clear_cache()
    ctx.store.record(s.email, "sso.update", provider, ", ".join(changed) or "no changes")
    view = sso.public_view(ctx.db, ctx.store.secrets, provider)
    view["redirect_uri"] = redirect_uri(request, provider)
    return view


@router.delete("/sso/{provider}")
def settings_remove(provider: str, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    if not sso.remove(ctx.db, _provider(provider)):
        raise HTTPException(404, "Nothing is saved for that provider.")
    sso.clear_cache()
    ctx.store.record(s.email, "sso.remove", provider)
    return {"ok": True}


# ---- admin: unlink a person's account -------------------------------------------------------------------
@router.post("/users/{uid}/sso/{provider}/unlink")
def unlink(uid: int, provider: str, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    if uid == s.user_id:
        raise HTTPException(400, "Ask another Admin to unlink your own account.")
    try:
        person = users.unlink_sso(ctx.db, uid, _provider(provider))
    except ValueError as e:
        raise HTTPException(422, str(e))
    ctx.store.record(s.email, "user.sso_unlink", person["email"], f"{sso.PROVIDERS[provider]['label']} account unlinked; sessions ended")
    return users.public(person)
