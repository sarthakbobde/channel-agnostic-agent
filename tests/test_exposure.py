"""What a public tunnel or reverse proxy exposes. Requests are made the way cloudflared (or any proxy) forwards them:
the connection comes from 127.0.0.1, the Host header is the public name and proxy headers are added. Every route of the
real application is tried with no credentials."""
import json
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app.admin import users
from app.api import app
from app.db import get_db

TUNNEL = {"host": "random-words.trycloudflare.com", "cf-connecting-ip": "203.0.113.9", "cf-ray": "abc-BOM",
          "x-forwarded-for": "203.0.113.9", "x-forwarded-proto": "https"}
# Reachable by anyone on purpose: the health check. Login and set-password pages answer only with a password or one-time link.
PUBLIC_OK = {"/health"}
SEND_NOTHING = {"/chat-auth/login", "/chat-auth/set-password"}


def routes(items=None, prefix=""):
    """Every (method, path) of the application, including routers added with include_router."""
    for r in (app.routes if items is None else items):
        inner = getattr(r, "original_router", None)
        if inner is not None:
            yield from routes(inner.routes, prefix + (getattr(r.include_context, "prefix", "") or ""))
        elif getattr(r, "methods", None) and getattr(r, "path", None) is not None:
            for m in r.methods - {"HEAD", "OPTIONS"}:
                yield m, prefix + r.path


@pytest.fixture(scope="module")
def tunnel():
    return TestClient(app, base_url="http://random-words.trycloudflare.com", client=("127.0.0.1", 50000), raise_server_exceptions=False)


def fill(path: str) -> str:
    out = []
    for part in path.split("/"):
        out.append("x" if part.startswith("{") else part)
    return "/".join(out)


def test_every_route_refuses_an_anonymous_request_that_arrives_through_a_tunnel(tunnel):
    open_routes = []
    seen = 0
    for method, path in routes():
        seen += 1
        r = tunnel.request(method, fill(path), headers=TUNNEL, json={} if method in ("POST", "PUT", "PATCH") else None)
        if 200 <= r.status_code < 300 and path not in PUBLIC_OK:
            open_routes.append((method, path, r.status_code))
    assert seen > 100
    assert open_routes == [], f"answered an anonymous tunnel request: {open_routes}"


def test_the_health_check_reveals_only_a_status(tunnel):
    r = tunnel.get("/health", headers=TUNNEL)
    assert r.status_code == 200 and set(r.json()) == {"status", "guardrails"}


def test_documentation_pages_are_local_only(tunnel):
    for p in ("/docs", "/openapi.json"):
        assert tunnel.get(p, headers=TUNNEL).status_code == 403
    local = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    assert local.get("/openapi.json").status_code == 200


def test_the_chat_api_needs_its_token_even_from_the_tunnel_host(tunnel):
    assert tunnel.post("/chat", headers=TUNNEL, json={"message": "hi", "conv_id": "c"}).status_code == 401
    assert tunnel.get("/conversations", headers=TUNNEL).status_code == 401
    assert tunnel.get("/conversations/c", headers=TUNNEL).status_code == 401
    assert tunnel.delete("/conversations/c", headers=TUNNEL).status_code == 401
    assert tunnel.post("/chat", headers={**TUNNEL, "authorization": "Bearer wrong"}, json={"message": "hi", "conv_id": "c"}).status_code == 401


def test_a_signed_in_admin_session_does_not_work_through_the_tunnel():
    """Even with a genuine session cookie, the admin API and page refuse requests that arrive via a proxy."""
    db = get_db()
    users.create_admin(db, "tunnel-test@sysfore.com", "correct horse battery", domain="sysfore.com")
    local = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    r = local.post("/admin/api/auth/login", json={"email": "tunnel-test@sysfore.com", "password": "correct horse battery"})
    assert r.status_code == 200
    assert local.get("/admin/api/auth/me").status_code == 200
    cookie = dict(local.cookies)
    via = TestClient(app, base_url="http://random-words.trycloudflare.com", client=("127.0.0.1", 50000), cookies=cookie)
    for path in ("/admin/api/auth/me", "/admin/api/users", "/admin/api/channels", "/admin/api/audit/search", "/admin/api/reports/channels", "/admin"):
        assert via.get(path, headers=TUNNEL).status_code == 403, path


def test_the_admin_page_is_unreachable_with_only_a_forwarded_header(tunnel):
    """Forwarded headers alone (a proxy on the same machine that keeps the local Host name) are also enough to refuse."""
    local_host = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    for h in ("x-forwarded-for", "x-real-ip", "cf-connecting-ip", "forwarded", "x-forwarded-host"):
        assert local_host.get("/admin/api/auth/me", headers={h: "203.0.113.9"}).status_code == 403, h


def test_with_a_separate_admin_port_the_main_app_has_no_admin_route_at_all():
    code = ("import json; from app.api import app; "
            "print(json.dumps([getattr(r,'path','') for r in app.routes]))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180,
                         env={**os.environ, "ADMIN_PORT": "8765"})
    assert out.returncode == 0, out.stderr[-500:]
    paths = json.loads(out.stdout.strip().splitlines()[-1])
    assert not [p for p in paths if p.startswith("/admin")], [p for p in paths if p.startswith("/admin")]
    assert "/chat" in paths and "/health" in paths


def test_the_separate_admin_app_exposes_only_admin_routes_and_a_health_check():
    from app.admin.server import build_app
    paths = {getattr(r, "path", "") for r in build_app().routes}
    other = [p for p in paths if p and not (p.startswith("/admin") or p in ("/", "/health"))]
    assert other == [], other
    assert not any(p in paths for p in ("/chat", "/conversations", "/channels/teams/messages", "/channels/gchat/events"))
