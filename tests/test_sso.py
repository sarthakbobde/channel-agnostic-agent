"""Sign in with Microsoft or Google, against fake providers: real RSA-signed ID tokens, a fake discovery
document, key set and token endpoint. Nothing here talks to Microsoft or Google."""
import json
import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.admin import sso, sso_api, users
from tests.test_admin import EMAIL, env, login  # noqa: F401  (env is a fixture)

TENANT = "11111111-2222-3333-4444-555555555555"
MS_ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
CLIENT = "client-123"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(key, kid):
    d = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**d, "kid": kid, "use": "sig", "alg": "RS256"}


class FakeIdp:
    """Stands in for both providers. `claims` is what the next ID token says; tests change it."""

    def __init__(self, monkeypatch):
        self.claims, self.key, self.kid, self.posts = {}, KEY, "k1", []
        sso.clear_cache()
        monkeypatch.setattr(sso, "_get_json", self.get_json)
        monkeypatch.setattr(sso, "_post_form", self.post_form)

    def get_json(self, url):
        if url.endswith("openid-configuration"):
            ms = "microsoftonline" in url
            base = "https://idp.test/" + ("ms" if ms else "google")
            return {"issuer": MS_ISSUER if ms else "https://accounts.google.com", "authorization_endpoint": base + "/authorize",
                    "token_endpoint": base + "/token", "jwks_uri": base + "/keys"}
        if url.endswith("/keys"):
            return {"keys": [_jwk(KEY, "k1")]}
        raise AssertionError(url)

    def post_form(self, url, data):
        self.posts.append(data)
        now = int(time.time())
        body = {"aud": CLIENT, "iat": now, "exp": now + 600, **self.claims}
        return {"id_token": jwt.encode(body, self.key, algorithm="RS256", headers={"kid": self.kid})}


@pytest.fixture
def idp(monkeypatch):
    return FakeIdp(monkeypatch)


def _client(env):  # noqa: F811
    ctx, c = env
    c.app.include_router(sso_api.router)
    return ctx, c


def _configure(ctx, provider="microsoft", extra=TENANT):
    sso.save(ctx.db, ctx.store.secrets, provider, enabled=True, client_id=CLIENT, client_secret="shh-secret", extra=extra)


def _ms_claims(nonce, **over):
    return {"iss": MS_ISSUER, "sub": "pairwise-sub", "tid": TENANT, "oid": "oid-1", "preferred_username": EMAIL,
            "nonce": nonce, **over}


def _google_claims(nonce, **over):
    return {"iss": "https://accounts.google.com", "sub": "g-1", "email": EMAIL, "email_verified": True,
            "hd": "sysfore.com", "nonce": nonce, **over}


def _start(c, provider="microsoft"):
    r = c.get(f"/admin/api/auth/sso/{provider}/start", follow_redirects=False)
    assert r.status_code == 302, r.text
    q = {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}
    return r, q


def _callback(c, q, provider="microsoft", **params):
    params = {"code": "the-code", "state": q["state"], **params}
    return c.get(f"/admin/api/auth/sso/{provider}/callback", params=params, follow_redirects=False)


def _error(r):
    return parse_qs(urlparse(r.headers["location"]).query).get("sso_error", [""])[0]


# ---- settings and the sign-in page --------------------------------------------------------------------------
def test_no_buttons_until_a_provider_is_set_up_and_switched_on(env):  # noqa: F811
    ctx, c = _client(env)
    assert c.get("/admin/api/auth/sso").json() == {"providers": []}
    assert _error(c.get("/admin/api/auth/sso/microsoft/start", follow_redirects=False)) == "unavailable"
    sso.save(ctx.db, ctx.store.secrets, "google", enabled=False, client_id=CLIENT, client_secret="x", extra="sysfore.com")
    assert c.get("/admin/api/auth/sso").json() == {"providers": []}
    _configure(ctx)
    assert c.get("/admin/api/auth/sso").json() == {"providers": [{"id": "microsoft", "label": "Microsoft"}]}


def test_settings_are_admin_only_secret_is_write_only_and_audited(env):  # noqa: F811
    ctx, c = _client(env)
    assert c.get("/admin/api/sso").status_code == 401
    _, hdr = login(c)
    r = c.put("/admin/api/sso/microsoft", json={"enabled": True, "client_id": CLIENT, "client_secret": "shh-secret", "extra": TENANT}, headers=hdr)
    assert r.status_code == 200 and r.json()["secret_set"] and r.json()["ready"]
    assert r.json()["redirect_uri"] == "http://localhost/admin/api/auth/sso/microsoft/callback"
    page = c.get("/admin/api/sso")
    assert "shh-secret" not in page.text and page.json()["providers"][0]["client_id"] == CLIENT
    # an empty secret keeps the saved one
    assert c.put("/admin/api/sso/microsoft", json={"enabled": True, "client_id": CLIENT, "client_secret": "", "extra": TENANT}, headers=hdr).status_code == 200
    assert sso.load(ctx.db, ctx.store.secrets, "microsoft").client_secret == "shh-secret"
    assert c.put("/admin/api/sso/microsoft", json={"enabled": True}, headers={}).status_code == 403            # CSRF
    audit = [(e["action"], e["detail"]) for e in ctx.store.recent_audit()]
    assert ("sso.update", "enabled, client ID, Directory (tenant) ID, client secret") in audit
    assert not any("shh-secret" in d for _, d in audit)
    assert c.delete("/admin/api/sso/microsoft", headers=hdr).status_code == 200
    assert c.get("/admin/api/auth/sso").json() == {"providers": []}


def test_settings_reject_incomplete_or_multi_tenant(env):  # noqa: F811
    ctx, c = _client(env)
    _, hdr = login(c)
    bad = [{"enabled": True, "client_id": CLIENT, "client_secret": "s", "extra": ""},
           {"enabled": True, "client_id": "", "client_secret": "s", "extra": TENANT},
           {"enabled": True, "client_id": CLIENT, "client_secret": "s", "extra": "common"}]
    for body in bad:
        assert c.put("/admin/api/sso/microsoft", json=body, headers=hdr).status_code == 422
    assert c.put("/admin/api/sso/github", json={}, headers=hdr).status_code == 404


def test_editors_cannot_change_sign_in_settings(env):  # noqa: F811
    ctx, c = _client(env)
    users.create_user(ctx.db, "ed@sysfore.com", "Ed", "editor", None, password="editor password 1")
    _, hdr = login(c, "ed@sysfore.com", "editor password 1")
    assert c.get("/admin/api/sso").status_code == 403
    assert c.put("/admin/api/sso/google", json={}, headers=hdr).status_code == 403


# ---- the flow --------------------------------------------------------------------------------------------------
def test_microsoft_sign_in_links_the_account_and_opens_the_panel(env, idp):  # noqa: F811
    ctx, c = _client(env)
    _configure(ctx)
    r, q = _start(c)
    assert r.headers["location"].startswith("https://idp.test/ms/authorize?")
    assert q["client_id"] == CLIENT and q["code_challenge_method"] == "S256" and q["scope"] == "openid email profile"
    assert q["redirect_uri"] == "http://localhost/admin/api/auth/sso/microsoft/callback"
    assert "samesite=lax" in r.headers["set-cookie"].lower() and "httponly" in r.headers["set-cookie"].lower()
    idp.claims = _ms_claims(q["nonce"])
    back = _callback(c, q)
    assert back.status_code == 303 and back.headers["location"] == "/admin"
    assert idp.posts[0]["code_verifier"] and idp.posts[0]["client_secret"] == "shh-secret"
    assert c.get("/admin/api/auth/me").json()["email"] == EMAIL                        # signed in
    assert users.get_user(ctx.db, EMAIL)["sso_microsoft"] == f"{TENANT}:oid-1"
    actions = [e["action"] for e in ctx.store.recent_audit()]
    assert "user.sso_link" in actions and "login" in actions


def test_after_linking_the_account_id_is_used_not_the_email(env, idp):  # noqa: F811
    ctx, c = _client(env)
    _configure(ctx)
    _, q = _start(c)
    idp.claims = _ms_claims(q["nonce"])
    _callback(c, q)
    c.cookies.clear()
    # same person, email renamed in Entra: still signs in to the same account
    _, q = _start(c)
    idp.claims = _ms_claims(q["nonce"], preferred_username="renamed@sysfore.com")
    assert _callback(c, q).headers["location"] == "/admin"
    assert c.get("/admin/api/auth/me").json()["email"] == EMAIL
    c.cookies.clear()
    # a different Entra account using the same email address is refused
    _, q = _start(c)
    idp.claims = _ms_claims(q["nonce"], oid="oid-2")
    assert _error(_callback(c, q)) == "linked_elsewhere"


def test_google_sign_in_checks_domain_and_verified_email(env, idp):  # noqa: F811
    ctx, c = _client(env)
    _configure(ctx, "google", "sysfore.com")
    r, q = _start(c, "google")
    assert q["hd"] == "sysfore.com"
    idp.claims = _google_claims(q["nonce"], hd="gmail.com")
    assert _error(_callback(c, q, "google")) == "wrong_domain"
    _, q = _start(c, "google")
    idp.claims = _google_claims(q["nonce"], email_verified=False)
    assert _error(_callback(c, q, "google")) == "no_email"
    _, q = _start(c, "google")
    idp.claims = _google_claims(q["nonce"])
    assert _callback(c, q, "google").headers["location"] == "/admin"
    assert users.get_user(ctx.db, EMAIL)["sso_google"] == "g-1"


def test_people_not_in_the_list_viewers_and_deactivated_are_refused(env, idp):  # noqa: F811
    ctx, c = _client(env)
    _configure(ctx)
    _, q = _start(c)
    idp.claims = _ms_claims(q["nonce"], preferred_username="stranger@sysfore.com")
    assert _error(_callback(c, q)) == "not_invited"
    users.create_user(ctx.db, "viewer@sysfore.com", "V", "viewer", None)
    _, q = _start(c)
    idp.claims = _ms_claims(q["nonce"], oid="oid-v", preferred_username="viewer@sysfore.com")
    assert _error(_callback(c, q)) == "not_allowed"
    assert users.get_user(ctx.db, "viewer@sysfore.com")["sso_microsoft"] is None          # nothing linked
    assert c.get("/admin/api/auth/me").status_code == 401
    assert any(e["action"] == "login.sso_denied" for e in ctx.store.recent_audit())


@pytest.mark.parametrize("change", ["wrong_state", "no_cookie", "wrong_nonce", "wrong_audience", "wrong_issuer",
                                    "expired", "bad_signature", "unknown_key", "other_tenant"])
def test_tampered_or_replayed_responses_are_refused(env, idp, change):  # noqa: F811
    ctx, c = _client(env)
    _configure(ctx)
    _, q = _start(c)
    idp.claims = _ms_claims(q["nonce"])
    params = {}
    if change == "wrong_state":
        params["state"] = "x" + q["state"]
    elif change == "no_cookie":
        c.cookies.clear()
    elif change == "wrong_nonce":
        idp.claims["nonce"] = "other"
    elif change == "wrong_audience":
        idp.claims["aud"] = "someone-else"
    elif change == "wrong_issuer":
        idp.claims["iss"] = "https://login.microsoftonline.com/other/v2.0"
    elif change == "expired":
        idp.claims.update(iat=int(time.time()) - 7200, exp=int(time.time()) - 3600)
    elif change == "bad_signature":
        idp.key = OTHER_KEY
    elif change == "unknown_key":
        idp.kid = "k9"
    elif change == "other_tenant":
        idp.claims.update(iss="https://login.microsoftonline.com/99999999-0000-0000-0000-000000000000/v2.0")
    r = _callback(c, q, **params)
    assert r.status_code == 303 and _error(r) in ("expired", "failed")
    assert c.get("/admin/api/auth/me").status_code == 401
    assert users.get_user(ctx.db, EMAIL)["sso_microsoft"] is None


def test_cancel_at_the_provider_returns_quietly(env, idp):  # noqa: F811
    ctx, c = _client(env)
    _configure(ctx)
    _, q = _start(c)
    r = c.get("/admin/api/auth/sso/microsoft/callback", params={"state": q["state"], "error": "access_denied"}, follow_redirects=False)
    assert _error(r) == "cancelled" and not idp.posts


def test_state_cookie_is_not_an_admin_session(env, idp):  # noqa: F811
    ctx, c = _client(env)
    _configure(ctx)
    r, _ = _start(c)
    token = r.cookies.get(sso_api.STATE_COOKIE) or c.cookies.get(sso_api.STATE_COOKIE)
    assert token and ctx.signer.read(token) is None


# ---- sessions --------------------------------------------------------------------------------------------------
def test_password_still_works_and_old_sessions_survive_the_upgrade(env):  # noqa: F811
    from app.admin.auth import fingerprint, session_fingerprint
    ctx, c = _client(env)
    u = users.get_user(ctx.db, EMAIL)
    assert session_fingerprint(u) == fingerprint(u["password_hash"])            # version 0: same cookie as before
    assert login(c)[0].status_code == 200


def test_unlink_ends_sessions_and_blocks_sso_only_people(env, idp):  # noqa: F811
    ctx, c = _client(env)
    _configure(ctx)
    other = users.create_user(ctx.db, "sso.only@sysfore.com", "S", "editor", None)       # no password
    _, q = _start(c)
    idp.claims = _ms_claims(q["nonce"], oid="oid-s", preferred_username="sso.only@sysfore.com")
    assert _callback(c, q).headers["location"] == "/admin"
    assert c.get("/admin/api/auth/me").json()["email"] == "sso.only@sysfore.com"
    sso_cookie = c.cookies.get("rag_admin")
    c.cookies.clear()
    _, hdr = login(c)                                                                  # the Admin, with a password
    assert c.post(f"/admin/api/users/{other['id']}/sso/microsoft/unlink", headers=hdr).status_code == 200
    assert users.get_user(ctx.db, "sso.only@sysfore.com")["sso_microsoft"] is None
    c.cookies.clear()
    c.cookies.set("rag_admin", sso_cookie, path="/admin")
    assert c.get("/admin/api/auth/me").status_code == 401                              # their session ended


def test_cannot_unlink_yourself(env):  # noqa: F811
    ctx, c = _client(env)
    _, hdr = login(c)
    me = users.get_user(ctx.db, EMAIL)
    assert c.post(f"/admin/api/users/{me['id']}/sso/microsoft/unlink", headers=hdr).status_code == 400


def test_user_list_shows_sign_in_methods_without_account_ids(env):  # noqa: F811
    ctx, c = _client(env)
    from app.admin import users_api
    c.app.include_router(users_api.router)
    with ctx.db.session() as s:
        from app.db import User
        s.query(User).filter_by(email=EMAIL).one().sso_google = "g-secret-sub"
    login(c)
    row = [u for u in c.get("/admin/api/users").json() if u["email"] == EMAIL][0]
    assert row["sign_in"] == ["local", "google"]
    assert "g-secret-sub" not in json.dumps(row) and "session_version" not in row
