"""The real application: no route answers an anonymous request, the chat API needs its token,
and the built-in API docs are local-only. This is the guard for BRD gap #65."""
import dataclasses
import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import app.api as api_module
from app import chat_service, memory, security

TOKEN = "test-chat-token"                       # set by tests/conftest.py
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SAFE_REFUSALS = {401, 403, 404, 405, 422, 503}


def iter_routes(routes, prefix=""):
    """(path, methods) for every route, including those mounted through include_router."""
    for r in routes:
        if isinstance(r, APIRoute):
            yield prefix + r.path, sorted(r.methods - {"HEAD", "OPTIONS"})
        elif hasattr(r, "original_router"):
            ctx = getattr(r, "include_context", None)
            yield from iter_routes(r.original_router.routes, prefix + (getattr(ctx, "prefix", "") or ""))


@pytest.fixture(scope="module")
def client():
    return TestClient(api_module.app, raise_server_exceptions=False)


def test_the_enumeration_really_finds_every_kind_of_route():
    paths = {p for p, _ in iter_routes(api_module.app.routes)}
    assert {"/health", "/chat", "/conversations", "/conversations/{conv_id}", "/docs", "/openapi.json",
            "/channels/teams/messages", "/channels/gchat/events", "/admin", "/admin/static/{name}",
            "/admin/api/auth/login", "/admin/api/channels", "/admin/api/settings", "/admin/api/audit"} <= paths
    assert len(paths) >= 20          # guards against enumerating nothing (22 at the time of writing)


def test_no_route_answers_an_anonymous_request_except_health(client):
    offenders = []
    for path, methods in iter_routes(api_module.app.routes):
        url = re.sub(r"\{[^}]+\}", "x", path)
        for method in methods:
            body = {"json": {}} if method in ("POST", "PUT") else {}
            status = client.request(method, url, **body).status_code
            if path == "/health":
                assert status == 200
            elif status not in SAFE_REFUSALS:
                offenders.append(f"{method} {path} -> {status}")
    assert not offenders, "anonymous requests were not refused: " + "; ".join(offenders)


@pytest.mark.parametrize("method, url, body", [
    ("POST", "/chat", {"message": "hi", "conv_id": "c1"}),
    ("GET", "/conversations", None),
    ("GET", "/conversations/c1", None),
    ("DELETE", "/conversations/c1", None),
])
def test_chat_routes_need_the_service_token(client, method, url, body):
    kwargs = {"json": body} if body else {}
    assert client.request(method, url, **kwargs).status_code == 401
    assert client.request(method, url, headers={"Authorization": "Bearer wrong"}, **kwargs).status_code == 401
    assert client.request(method, url, headers={"Authorization": f"Bearer {TOKEN}x"}, **kwargs).status_code == 401
    assert client.request(method, url, headers={"Authorization": f"Bearer {TOKEN[:-1]}"}, **kwargs).status_code == 401
    assert client.request(method, url, headers={"Authorization": f"Basic {TOKEN}"}, **kwargs).status_code == 401
    assert client.request(method, url, headers={"Authorization": TOKEN}, **kwargs).status_code == 401


def test_chat_routes_work_with_the_token(client, monkeypatch):
    monkeypatch.setattr(chat_service, "chat_stream",
                        lambda m, c: (iter(["Hello ", "there"]), {"sources": [], "searched": False, "query": ""}))
    monkeypatch.setattr(memory, "list_conversations", lambda: [{"id": "c1", "title": "hi"}])
    monkeypatch.setattr(memory, "get_messages", lambda c: [{"role": "user", "content": "hi", "meta": {}}])
    deleted = []
    monkeypatch.setattr(memory, "delete", lambda c: deleted.append(c))
    r = client.post("/chat", headers=AUTH, json={"message": "hi", "conv_id": "c1"})
    assert r.status_code == 200 and r.json()["answer"] == "Hello there"
    assert client.get("/conversations", headers=AUTH).json() == [{"id": "c1", "title": "hi"}]
    assert client.get("/conversations/c1", headers=AUTH).json()["messages"][0]["content"] == "hi"
    assert client.delete("/conversations/c1", headers=AUTH).status_code == 200 and deleted == ["c1"]


def test_the_api_stays_locked_when_no_token_is_configured(client, monkeypatch):
    monkeypatch.setattr(security, "settings", dataclasses.replace(security.settings, chat_api_token=""))
    for headers in ({}, AUTH, {"Authorization": "Bearer "}):
        r = client.get("/conversations", headers=headers)
        assert r.status_code == 503 and "CHAT_API_TOKEN" in r.json()["detail"]


def test_chat_errors_do_not_leak_details(client, monkeypatch):
    def boom(m, c):
        raise RuntimeError("secret-internal-detail")
    monkeypatch.setattr(chat_service, "chat_stream", boom)
    r = client.post("/chat", headers=AUTH, json={"message": "hi", "conv_id": "c1"})
    assert r.status_code == 500 and "secret-internal-detail" not in r.text


# ---- API docs: local only ----------------------------------------------------------------
@pytest.fixture
def local_client(monkeypatch):
    # TestClient's peer is "testclient"; treat it as loopback to test the local-only rules
    monkeypatch.setattr("app.admin.api.LOOPBACK", {"127.0.0.1", "::1", "localhost", "testclient"})
    return TestClient(api_module.app, base_url="http://localhost:8000", raise_server_exceptions=False)


def test_docs_and_schema_are_available_locally(local_client):
    schema = local_client.get("/openapi.json")
    assert schema.status_code == 200 and "/chat" in schema.json()["paths"]
    assert "bearer" in str(schema.json()["components"]["securitySchemes"]).lower()    # Swagger "Authorize" works
    docs = local_client.get("/docs")
    assert docs.status_code == 200 and "swagger" in docs.text.lower()
    assert local_client.get("/redoc").status_code == 404


@pytest.mark.parametrize("headers", [{"cf-connecting-ip": "1.2.3.4"}, {"x-forwarded-for": "1.2.3.4"},
                                     {"host": "abc.trycloudflare.com"}])
def test_docs_are_refused_through_a_tunnel_or_proxy(local_client, headers):
    assert local_client.get("/docs", headers=headers).status_code == 403
    assert local_client.get("/openapi.json", headers=headers).status_code == 403


def test_docs_stay_local_even_when_remote_admin_is_allowed(local_client, monkeypatch):
    import app.admin.api as admin_api
    monkeypatch.setattr("app.api.settings", dataclasses.replace(api_module.settings, admin_allow_remote=True))
    assert local_client.get("/docs", headers={"x-forwarded-for": "1.2.3.4"}).status_code == 403
