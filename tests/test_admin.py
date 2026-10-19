"""Admin API tests: sign-in, sessions, CSRF, local-only guard, channels, audit, admins."""
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.admin import api as admin_api
from app.admin import users
from app.admin.auth import LoginLimiter, SessionSigner, hash_password, verify_password
from app.channels.store import ChannelStore
from app.secretstore import FernetSecretStore, generate_key
from tests.test_channels import FakeAdapter, FakeManager

EMAIL = "boss@sysfore.com"
PASSWORD = "correct horse battery"
SECRET = "super-secret-token-value"


# ---- low-level helpers --------------------------------------------------------
def test_password_hash_roundtrip():
    h = hash_password("a long password!")
    assert "a long password" not in h and verify_password("a long password!", h)
    assert not verify_password("wrong", h) and not verify_password("x", "garbage")


def test_session_signer_rejects_tampering_and_expiry():
    s = SessionSigner(generate_key(), ttl=60)
    token, csrf = s.issue("a@sysfore.com", "fp")
    assert s.read(token)["sub"] == "a@sysfore.com" and s.read(token)["csrf"] == csrf
    body, sig = token.rsplit(".", 1)
    assert s.read(body + "x." + sig) is None and s.read(token + "x") is None and s.read("junk") is None
    assert SessionSigner(generate_key(), 60).read(token) is None          # other key


def test_expired_token_is_rejected():
    s = SessionSigner(generate_key(), ttl=-1)
    token, _ = s.issue("a@sysfore.com", "fp")
    assert s.read(token) is None


def test_limiter_blocks_then_expires():
    lim = LoginLimiter(max_failures=3, window=1)
    for _ in range(3):
        lim.failure("k")
    assert lim.blocked("k")
    time.sleep(1.1)
    assert not lim.blocked("k")
    lim.failure("k"); lim.success("k")
    assert not lim.blocked("k")


def _req(client_host, headers):
    scope = {"type": "http", "client": (client_host, 1234), "method": "GET", "path": "/",
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    return Request(scope)


def test_check_local_guard():
    admin_api.check_local(_req("127.0.0.1", {"host": "localhost:8000"}), False)
    admin_api.check_local(_req("::1", {"host": "127.0.0.1:8000"}), False)
    for bad in (_req("10.0.0.5", {"host": "localhost:8000"}),                       # other machine
                _req("127.0.0.1", {"host": "abc.trycloudflare.com"}),               # tunnel host
                _req("127.0.0.1", {"host": "localhost", "cf-connecting-ip": "1.2.3.4"}),
                _req("127.0.0.1", {"host": "localhost", "x-forwarded-for": "1.2.3.4"})):
        with pytest.raises(HTTPException) as e:
            admin_api.check_local(bad, False)
        assert e.value.status_code == 403
    admin_api.check_local(_req("10.0.0.5", {"host": "x"}), True)                    # explicit opt-in


# ---- app fixture --------------------------------------------------------------
@pytest.fixture
def env(new_db):
    FakeAdapter.instances = []
    db = new_db()
    secrets = FernetSecretStore(generate_key())
    store = ChannelStore(db, secrets, lambda t: FakeAdapter.SPEC)
    mgr = FakeManager({"fake": ("x", "y")})
    ctx = admin_api.AdminContext(db=db, store=store, manager=mgr, signer=SessionSigner(generate_key()),
                                 domain="sysfore.com", allow_remote=True)
    users.create_admin(db, EMAIL, PASSWORD, domain="sysfore.com")
    app = FastAPI()
    app.include_router(admin_api.router)
    app.dependency_overrides[admin_api.get_ctx] = lambda: ctx
    return ctx, TestClient(app, base_url="http://localhost")


def login(c, email=EMAIL, password=PASSWORD):
    r = c.post("/admin/api/auth/login", json={"email": email, "password": password})
    return r, ({"X-CSRF-Token": r.json()["csrf"]} if r.status_code == 200 else {})


# ---- auth ------------------------------------------------------------------
def test_login_success_sets_hardened_cookie(env):
    ctx, c = env
    r, _ = login(c)
    assert r.status_code == 200 and r.json()["email"] == EMAIL
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/admin" in cookie
    assert c.get("/admin/api/auth/me").json()["email"] == EMAIL


def test_login_failures_are_generic_and_audited(env):
    ctx, c = env
    bad_pw, _ = login(c, password="wrong password!!")
    bad_user, _ = login(c, email="nobody@sysfore.com")
    assert bad_pw.status_code == bad_user.status_code == 401
    assert bad_pw.json() == bad_user.json()                          # cannot tell which was wrong
    assert any(e["action"] == "login.failed" for e in ctx.store.recent_audit())


def test_lockout_after_repeated_failures(env):
    ctx, c = env
    for _ in range(5):
        assert login(c, password="wrong password!!")[0].status_code == 401
    assert login(c)[0].status_code == 429                            # even the right password
    assert login(c, email="other@sysfore.com")[0].status_code == 401  # different account unaffected


def test_endpoints_require_sign_in(env):
    _, c = env
    for method, path in [("get", "/channels"), ("get", "/audit"), ("get", "/admins"),
                         ("put", "/channels/fake"), ("delete", "/channels/fake"),
                         ("post", "/channels/fake/test"), ("post", "/admins")]:
        r = getattr(c, method)("/admin/api" + path, **({"json": {}} if method in ("put", "post") else {}))
        assert r.status_code == 401, path


def test_changes_need_csrf_token(env):
    _, c = env
    login(c)
    body = {"enabled": False, "values": {"token": "t"}}
    assert c.put("/admin/api/channels/fake", json=body).status_code == 403
    assert c.put("/admin/api/channels/fake", json=body, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert c.get("/admin/api/channels").status_code == 200           # reads need no token


def test_admin_api_unavailable_without_master_key():
    app = FastAPI()
    app.include_router(admin_api.router)

    def no_key():
        raise HTTPException(503, "Admin is disabled")
    app.dependency_overrides[admin_api.get_ctx] = no_key
    assert TestClient(app).post("/admin/api/auth/login", json={"email": "a", "password": "b"}).status_code == 503


# ---- channels -------------------------------------------------------------------
def test_create_enable_and_never_leak_secret(env):
    ctx, c = env
    _, h = login(c)
    r = c.put("/admin/api/channels/fake", headers=h, json={"enabled": True, "values": {"token": SECRET, "mode": "a"}})
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] and body["running"] and body["configured"]
    assert SECRET not in r.text and SECRET not in c.get("/admin/api/channels").text
    assert ctx.manager.get("fake").cfg["token"] == SECRET            # the runtime has it
    f = {x["name"]: x for x in body["fields"]}
    assert f["token"]["is_set"] is True and "value" not in f["token"] and f["mode"]["value"] == "a"


def test_unknown_setting_is_rejected_and_nothing_changes(env):
    ctx, c = env
    _, h = login(c)
    c.put("/admin/api/channels/fake", headers=h, json={"enabled": True, "values": {"token": "good-token"}})
    good = ctx.manager.get("fake")
    r = c.put("/admin/api/channels/fake", headers=h,
              json={"enabled": True, "values": {"token": SECRET, "explode": True}})
    assert r.status_code == 422                                      # 'explode' is not a setting
    assert ctx.store.runtime_config("fake")[1]["token"] == "good-token"
    assert ctx.manager.get("fake") is good and not good.stopped


def test_start_error_message_is_scrubbed_and_nothing_saved(env, monkeypatch):
    ctx, c = env
    _, h = login(c)
    c.put("/admin/api/channels/fake", headers=h, json={"enabled": True, "values": {"token": "good-token"}})
    good = ctx.manager.get("fake")

    def boom(self):
        raise RuntimeError(f"auth failed for {self.cfg['token']}")
    monkeypatch.setattr(FakeAdapter, "start", boom)
    r = c.put("/admin/api/channels/fake", headers=h, json={"enabled": True, "values": {"token": SECRET}})
    assert r.status_code == 400 and SECRET not in r.text and "***" in r.text
    assert ctx.store.runtime_config("fake")[1]["token"] == "good-token"      # not saved
    assert ctx.manager.get("fake") is good


def test_disable_stops_channel_and_blank_secret_keeps_value(env):
    ctx, c = env
    _, h = login(c)
    c.put("/admin/api/channels/fake", headers=h, json={"enabled": True, "values": {"token": SECRET}})
    running = ctx.manager.get("fake")
    r = c.put("/admin/api/channels/fake", headers=h, json={"enabled": False, "values": {"token": ""}})
    assert r.status_code == 200 and not r.json()["running"] and running.stopped
    assert ctx.store.runtime_config("fake") == (False, {"token": SECRET})


def test_invalid_and_unknown(env):
    _, c = env
    _, h = login(c)
    assert c.put("/admin/api/channels/fake", headers=h, json={"enabled": True, "values": {}}).status_code == 422
    assert c.put("/admin/api/channels/nope", headers=h, json={"enabled": True, "values": {}}).status_code == 404
    assert c.get("/admin/api/channels/nope").status_code == 404


def test_test_endpoint_audited_and_scrubbed(env, monkeypatch):
    ctx, c = env
    _, h = login(c)
    r = c.post("/admin/api/channels/fake/test", headers=h, json={"values": {"token": SECRET}})
    assert r.json()["ok"] is True
    monkeypatch.setattr(FakeAdapter, "test", lambda self: (False, f"rejected {self.cfg['token']}"))
    r = c.post("/admin/api/channels/fake/test", headers=h, json={"values": {"token": SECRET}})
    assert r.json()["ok"] is False and SECRET not in r.text
    detail = [e["detail"] for e in ctx.store.recent_audit() if e["action"] == "channel.test"]
    assert len(detail) == 2 and all(SECRET not in d for d in detail)


def test_delete_channel(env):
    ctx, c = env
    _, h = login(c)
    c.put("/admin/api/channels/fake", headers=h, json={"enabled": True, "values": {"token": SECRET}})
    assert c.delete("/admin/api/channels/fake", headers=h).status_code == 200
    assert ctx.manager.get("fake") is None and ctx.store.runtime_config("fake") is None
    assert c.delete("/admin/api/channels/fake", headers=h).status_code == 404


def test_audit_endpoint_has_actor_and_no_secrets(env):
    ctx, c = env
    _, h = login(c)
    c.put("/admin/api/channels/fake", headers=h, json={"enabled": True, "values": {"token": SECRET}})
    r = c.get("/admin/api/audit")
    assert SECRET not in r.text
    entries = r.json()
    assert any(e["action"] == "channel.create" and e["actor"] == EMAIL for e in entries)
    assert any(e["action"] == "login" for e in entries)


# ---- admins ---------------------------------------------------------------------
def test_admin_management_rules(env):
    ctx, c = env
    _, h = login(c)
    assert c.post("/admin/api/admins", headers=h, json={"email": "x@gmail.com", "password": PASSWORD}).status_code == 422
    assert c.post("/admin/api/admins", headers=h, json={"email": "new@sysfore.com", "password": "short"}).status_code == 422
    ok = c.post("/admin/api/admins", headers=h, json={"email": "New@Sysfore.com", "password": PASSWORD})
    assert ok.status_code == 201 and ok.json()["email"] == "new@sysfore.com" and "password_hash" not in ok.text
    assert c.post("/admin/api/admins", headers=h, json={"email": "new@sysfore.com", "password": PASSWORD}).status_code == 422
    listing = c.get("/admin/api/admins")
    assert "password" not in listing.text and len(listing.json()) == 2
    assert c.delete(f"/admin/api/admins/{EMAIL}", headers=h).status_code == 400      # not yourself
    assert c.delete("/admin/api/admins/new@sysfore.com", headers=h).status_code == 200
    assert c.delete("/admin/api/admins/new@sysfore.com", headers=h).status_code == 404


def test_cannot_remove_last_admin(env):
    ctx, _ = env
    with pytest.raises(ValueError, match="last admin"):
        users.delete_admin(ctx.db, EMAIL)


def test_password_change_signs_out_other_sessions(env):
    ctx, c = env
    _, h = login(c)
    old_cookie = c.cookies.get("rag_admin")
    bad = c.post("/admin/api/auth/password", headers=h, json={"current": "nope nope nope", "new": "a brand new password"})
    assert bad.status_code == 400
    ok = c.post("/admin/api/auth/password", headers=h, json={"current": PASSWORD, "new": "a brand new password"})
    assert ok.status_code == 200
    other = TestClient(c.app, base_url="http://localhost")
    other.cookies.set("rag_admin", old_cookie, path="/admin")
    assert other.get("/admin/api/auth/me").status_code == 401                         # old session dead
    assert c.get("/admin/api/auth/me").status_code == 200                              # current one renewed
    assert login(TestClient(c.app, base_url="http://localhost"), password="a brand new password")[0].status_code == 200


def test_logout_clears_session(env):
    _, c = env
    _, h = login(c)
    assert c.post("/admin/api/auth/logout", headers=h).status_code == 200
    assert c.get("/admin/api/auth/me").status_code == 401


# ---- global settings ---------------------------------------------------------------
def test_settings_need_sign_in_and_csrf(env):
    _, c = env
    assert c.get("/admin/api/settings").status_code == 401
    assert c.put("/admin/api/settings", json={"values": {"top_k": 3}}).status_code == 401
    _, h = login(c)
    assert c.put("/admin/api/settings", json={"values": {"top_k": 3}}).status_code == 403      # no CSRF token


def test_settings_view_update_and_audit(env):
    ctx, c = env
    _, h = login(c)
    view = c.get("/admin/api/settings").json()
    by = {f["name"]: f for f in view["fields"]}
    assert set(by) == {"top_k", "score_threshold", "recent_turns", "chunk_size", "chunk_overlap", "max_retention_days", "prompt_max_chars", "summarise_after_turns", "retention_job_enabled", "export_enabled", "chat_rate_per_minute"}
    assert (by["top_k"]["value"], by["score_threshold"]["value"], by["recent_turns"]["value"]) == (5, 0.35, 6)
    assert all(f["kind"] in ("number", "bool") and f["help"] for f in view["fields"])

    r = c.put("/admin/api/settings", headers=h, json={"values": {"top_k": 8, "score_threshold": 0.5}})
    assert r.status_code == 200
    by = {f["name"]: f["value"] for f in r.json()["fields"]}
    assert (by["top_k"], by["score_threshold"], by["recent_turns"]) == (8, 0.5, 6)           # others untouched
    assert r.json()["updated_by"] == EMAIL
    entry = [e for e in ctx.store.recent_audit() if e["action"] == "settings.update"][0]
    assert entry["actor"] == EMAIL and entry["detail"] == "top_k: 5 -> 8; score_threshold: 0.35 -> 0.5"
    again = c.put("/admin/api/settings", headers=h, json={"values": {"top_k": 8}})
    assert again.status_code == 200
    assert [e for e in ctx.store.recent_audit() if e["action"] == "settings.update"][0]["detail"] == "no changes"


@pytest.mark.parametrize("values, message", [
    ({"top_k": 0}, "between 1 and 50"),
    ({"top_k": 2.5}, "whole number"),
    ({"top_k": "5"}, "must be a number"),
    ({"top_k": True}, "must be a number"),
    ({"score_threshold": 1.5}, "between 0.0 and 1.0"),
    ({"chunk_overlap": 900}, "smaller than the chunk size"),
    ({"chunk_size": 100}, "smaller than the chunk size"),         # existing overlap 150 would no longer fit
    ({"nonsense": 1}, "Unknown setting"),
])
def test_settings_rejects_bad_values_and_changes_nothing(env, values, message):
    ctx, c = env
    _, h = login(c)
    r = c.put("/admin/api/settings", headers=h, json={"values": values})
    assert r.status_code == 422 and message in r.json()["detail"]
    assert {f["name"]: f["value"] for f in c.get("/admin/api/settings").json()["fields"]}["top_k"] == 5
