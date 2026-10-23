"""Sign in to the admin panel with Microsoft (Entra ID) or Google: OpenID Connect, authorization code flow
with PKCE, state and nonce.

Settings live in the database (Settings > Sign-in): the client ID, tenant or company domain and an on/off
switch in `meta`, the client secret encrypted in `secrets`. A provider shows on the sign-in page only when it
is switched on and complete, so until an app registration exists the page offers email and password only.

Checks on the ID token, beyond its signature (the provider's published keys), expiry and audience:
  * Microsoft: single tenant only. The issuer must be the configured tenant's, so accounts from other
    organisations and personal Microsoft accounts are refused.
  * Google: the email must be verified and the account must belong to the company domain (the `hd` claim).
Who may sign in is decided by users.sso_sign_in: only people already in the list (sign-on creates nobody).
"""
import base64
import hashlib
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import jwt
from sqlalchemy import select

from app.db import Database, Meta, Secret
from app.secretstore import SecretStore

log = logging.getLogger(__name__)
OWNER = "sso"                      # secrets.owner_type for the client secrets
SCOPES = "openid email profile"
HTTP_TIMEOUT = 10.0
CACHE_SECONDS = 3600
LEEWAY = 60                        # seconds of clock difference allowed on token times
MULTI_TENANT = {"common", "organizations", "consumers"}
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")

PROVIDERS = {
    "microsoft": {"label": "Microsoft", "extra": "tenant", "extra_label": "Directory (tenant) ID"},
    "google": {"label": "Google", "extra": "domain", "extra_label": "Company domain"},
}


class SsoError(Exception):
    """A sign-in that cannot go on. `code` is shown to the person as a short message on the sign-in page."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code, self.detail = code, detail


# ---- settings -------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    enabled: bool
    client_id: str
    client_secret: str
    extra: str                     # Microsoft: tenant ID; Google: company domain

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.client_id and self.client_secret and self.extra)


def _meta_key(provider: str) -> str:
    return "sso." + provider


def _check_provider(provider: str) -> None:
    if provider not in PROVIDERS:
        raise ValueError("Unknown sign-in provider.")


def load(db: Database, secrets_store: SecretStore, provider: str) -> ProviderConfig:
    _check_provider(provider)
    with db.session() as s:
        row = s.get(Meta, _meta_key(provider))
        secret = s.scalar(select(Secret).where(Secret.owner_type == OWNER, Secret.owner_id == provider,
                                               Secret.name == "client_secret"))
        cfg = json.loads(row.value) if row is not None and row.value else {}
        token = secret.ciphertext if secret is not None else None
    return ProviderConfig(provider, bool(cfg.get("enabled")), cfg.get("client_id", ""),
                          secrets_store.decrypt(token) if token else "", cfg.get("extra", ""))


def public_view(db: Database, secrets_store: SecretStore, provider: str) -> dict:
    """Safe for the admin screen: the secret is replaced by whether one is saved."""
    c = load(db, secrets_store, provider)
    return {"id": provider, "label": PROVIDERS[provider]["label"], "enabled": c.enabled, "ready": c.ready,
            "client_id": c.client_id, "secret_set": bool(c.client_secret), "extra": c.extra,
            "extra_name": PROVIDERS[provider]["extra"], "extra_label": PROVIDERS[provider]["extra_label"]}


def _clean_extra(provider: str, value: str) -> str:
    v = (value or "").strip().lower()
    if provider == "microsoft" and v in MULTI_TENANT:
        raise ValueError("Use your organisation's tenant ID: sign-in is limited to one organisation.")
    if provider == "google":
        v = v.lstrip("@")
    if v and (" " in v or "/" in v):
        raise ValueError(f"{PROVIDERS[provider]['extra_label']} is not valid.")
    return v


def save(db: Database, secrets_store: SecretStore, provider: str, *, enabled: bool, client_id: str,
         client_secret: str | None, extra: str) -> list[str]:
    """Store a provider's settings. An empty client secret keeps the saved one. Returns what changed."""
    _check_provider(provider)
    client_id, extra = (client_id or "").strip(), _clean_extra(provider, extra)
    before = load(db, secrets_store, provider)
    new_secret = (client_secret or "").strip()
    if enabled and not (client_id and extra and (new_secret or before.client_secret)):
        raise ValueError("Fill in every field before switching this on.")
    changed = [name for name, old, new in (("enabled", before.enabled, bool(enabled)), ("client ID", before.client_id, client_id),
                                           (PROVIDERS[provider]["extra_label"], before.extra, extra)) if old != new]
    with db.session() as s:
        value = json.dumps({"enabled": bool(enabled), "client_id": client_id, "extra": extra})
        row = s.get(Meta, _meta_key(provider))
        if row is None:
            s.add(Meta(key=_meta_key(provider), value=value))
        else:
            row.value = value
        if new_secret:
            sec = s.scalar(select(Secret).where(Secret.owner_type == OWNER, Secret.owner_id == provider,
                                                Secret.name == "client_secret"))
            token = secrets_store.encrypt(new_secret)
            if sec is None:
                s.add(Secret(owner_type=OWNER, owner_id=provider, name="client_secret", ciphertext=token))
            else:
                sec.ciphertext = token
            changed.append("client secret")
    return changed


def remove(db: Database, provider: str) -> bool:
    _check_provider(provider)
    with db.session() as s:
        row = s.get(Meta, _meta_key(provider))
        secs = list(s.scalars(select(Secret).where(Secret.owner_type == OWNER, Secret.owner_id == provider)))
        for x in ([row] if row is not None else []) + secs:
            s.delete(x)
        return row is not None or bool(secs)


def enabled_providers(db: Database, secrets_store: SecretStore) -> list[dict]:
    """The buttons for the sign-in page."""
    out = []
    for p in PROVIDERS:
        try:
            if load(db, secrets_store, p).ready:
                out.append({"id": p, "label": PROVIDERS[p]["label"]})
        except Exception:                       # e.g. a secret that cannot be decrypted after a key change
            log.warning("sign-in with %s is unavailable: its settings cannot be read", p, exc_info=True)
    return out


# ---- network (module functions, so tests can replace them) ----------------------------------------------
def _get_json(url: str) -> dict:
    r = httpx.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _post_form(url: str, data: dict) -> dict:
    r = httpx.post(url, data=data, timeout=HTTP_TIMEOUT, headers={"Accept": "application/json"})
    if r.status_code >= 400:
        try:
            err = r.json()
            reason = f"{err.get('error', '')}: {err.get('error_description', '')}"[:300]
        except ValueError:
            reason = f"HTTP {r.status_code}"
        raise SsoError("failed", "token exchange refused: " + reason)
    return r.json()


_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _cached_json(url: str, fresh: bool = False) -> dict:
    now = time.time()
    with _cache_lock:
        hit = _cache.get(url)
    if hit and not fresh and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    data = _get_json(url)
    with _cache_lock:
        _cache[url] = (now, data)
    return data


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ---- protocol ------------------------------------------------------------------------------------------
def discovery_url(cfg: ProviderConfig) -> str:
    if cfg.provider == "microsoft":
        return f"https://login.microsoftonline.com/{cfg.extra}/v2.0/.well-known/openid-configuration"
    return "https://accounts.google.com/.well-known/openid-configuration"


def _metadata(cfg: ProviderConfig) -> dict:
    try:
        meta = _cached_json(discovery_url(cfg))
    except (httpx.HTTPError, ValueError) as e:
        raise SsoError("failed", f"could not read the provider's sign-in settings: {type(e).__name__}") from None
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer"):
        if not meta.get(key):
            raise SsoError("failed", f"the provider's sign-in settings have no {key}")
    return meta


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def begin(cfg: ProviderConfig, redirect_uri: str) -> tuple[str, dict]:
    """The provider's sign-in URL, and what to keep (signed, in a short-lived cookie) for the callback."""
    meta = _metadata(cfg)
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    params = {"client_id": cfg.client_id, "response_type": "code", "redirect_uri": redirect_uri, "scope": SCOPES,
              "state": state, "nonce": nonce, "prompt": "select_account",
              "code_challenge": _b64url(hashlib.sha256(verifier.encode("ascii")).digest()),
              "code_challenge_method": "S256"}
    if cfg.provider == "microsoft":
        params["response_mode"] = "query"
    else:
        params["hd"] = cfg.extra                       # Google shows only accounts of this domain (checked again later)
    keep = {"p": cfg.provider, "s": state, "n": nonce, "v": verifier, "r": redirect_uri}
    return meta["authorization_endpoint"] + "?" + urlencode(params), keep


def _signing_key(meta: dict, kid: str):
    for fresh in (False, True):                      # keys rotate: fetch once more before giving up
        try:
            keys = _cached_json(meta["jwks_uri"], fresh=fresh).get("keys", [])
        except (httpx.HTTPError, ValueError) as e:
            raise SsoError("failed", f"could not read the provider's signing keys: {type(e).__name__}") from None
        for k in keys:
            if k.get("kid") == kid:
                return jwt.PyJWK(k).key
    raise SsoError("failed", "the ID token is signed with an unknown key")


def _expected_issuers(cfg: ProviderConfig, meta: dict) -> list[str]:
    if cfg.provider == "google":
        return list(GOOGLE_ISSUERS)
    issuer = meta["issuer"]
    if "{tenantid}" in issuer:                       # a multi-tenant endpoint: never accepted
        raise SsoError("failed", "the Microsoft tenant setting is not a single organisation")
    return [issuer]


def verify_id_token(cfg: ProviderConfig, meta: dict, id_token: str, nonce: str) -> dict:
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError:
        raise SsoError("failed", "the ID token cannot be read") from None
    if header.get("alg") != "RS256":
        raise SsoError("failed", f"unexpected ID token algorithm {header.get('alg')!r}")
    key = _signing_key(meta, header.get("kid", ""))
    try:
        claims = jwt.decode(id_token, key, algorithms=["RS256"], audience=cfg.client_id,
                            issuer=_expected_issuers(cfg, meta), leeway=LEEWAY,
                            options={"require": ["exp", "iat", "iss", "aud", "sub"]})
    except jwt.PyJWTError as e:
        raise SsoError("failed", f"the ID token is not valid: {type(e).__name__}") from None
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise SsoError("failed", "the ID token does not belong to this sign-in (nonce)")
    return claims


def identity(cfg: ProviderConfig, claims: dict) -> tuple[str, str]:
    """(stable account id, email) from verified claims, after the provider-specific checks."""
    if cfg.provider == "microsoft":
        tid, oid = claims.get("tid", ""), claims.get("oid", "")
        if not tid or not oid:
            raise SsoError("failed", "the Microsoft ID token has no tenant or object id")
        email = (claims.get("email") or claims.get("preferred_username") or "").strip().lower()
        if "@" not in email:
            raise SsoError("no_email", "the Microsoft account has no email address")
        return f"{tid}:{oid}", email
    email = (claims.get("email") or "").strip().lower()
    if claims.get("email_verified") is not True or "@" not in email:
        raise SsoError("no_email", "the Google account's email is not verified")
    domain = cfg.extra.lower()
    if str(claims.get("hd", "")).lower() != domain or not email.endswith("@" + domain):
        raise SsoError("wrong_domain", f"Google account outside {domain}")
    return str(claims["sub"]), email


def finish(cfg: ProviderConfig, keep: dict, code: str) -> tuple[str, str]:
    """Exchange the code (with the PKCE verifier) and return the verified (account id, email)."""
    meta = _metadata(cfg)
    tokens = _post_form(meta["token_endpoint"], {
        "grant_type": "authorization_code", "code": code, "redirect_uri": keep["r"],
        "client_id": cfg.client_id, "client_secret": cfg.client_secret, "code_verifier": keep["v"]})
    id_token = tokens.get("id_token")
    if not id_token:
        raise SsoError("failed", "the provider returned no ID token")
    return identity(cfg, verify_id_token(cfg, meta, id_token, keep["n"]))
