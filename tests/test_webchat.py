"""The Chainlit web chat signs people in through the API, sends each person's own session token, shows only their own
history, and lets Viewers choose a password from their one-time link. The API is faked with httpx.MockTransport."""
import asyncio
import json
import os
import pathlib
import sys

import httpx
import pytest

WEBCHAT = pathlib.Path(__file__).resolve().parents[1] / "webchat"
os.environ.setdefault("CHAINLIT_APP_ROOT", str(WEBCHAT))       # Chainlit reads its config from here at import
os.environ.setdefault("CHAINLIT_AUTH_SECRET", "test-secret-not-used-in-production")
sys.path.insert(0, str(WEBCHAT))

from chainlit.types import Feedback, Pagination, ThreadFilter  # noqa: E402

import backend  # noqa: E402

CONVS = {"amy-token": [{"id": "t1", "title": "leave policy", "updated_at": "2026-10-27T07:00:00+00:00"},
                       {"id": "t2", "title": "wfh rules", "updated_at": "2026-10-26T07:00:00+00:00"}],
         "bob-token": [{"id": "t9", "title": "bob's chat", "updated_at": None}]}


@pytest.fixture
def api(monkeypatch):
    """Fake API; .calls records (method, path, token, body)."""
    class Api:
        calls = []
        chat_status = 200
    a = Api()

    def handle(req: httpx.Request) -> httpx.Response:
        token = req.headers.get("authorization", "").removeprefix("Bearer ")
        body = json.loads(req.content) if req.content else None
        a.calls.append((req.method, req.url.path, token, body))
        if req.url.path == "/chat-auth/login":
            if body["password"] != "right password 1":
                return httpx.Response(401, json={"detail": "Invalid email or password."})
            return httpx.Response(200, json={"token": "amy-token", "email": body["email"], "name": "Amy", "role": "viewer"})
        if req.url.path == "/chat-auth/set-password":
            ok = body["token"] == "good-link"
            return httpx.Response(200 if ok else 400, json={} if ok else {"detail": "This link has expired."})
        if req.url.path == "/chat":
            return httpx.Response(a.chat_status, json={"answer": "18 earned leaves [1]", "searched": True, "query": "q",
                                                       "sources": [{"n": 1, "source": "HR Policy.pdf", "page": 4}]})
        if req.url.path == "/conversations":
            return httpx.Response(200 if token in CONVS else 401, json=CONVS.get(token, {"detail": "no"}))
        if req.url.path.startswith("/conversations/"):
            cid = req.url.path.rsplit("/", 1)[1]
            if cid not in [c["id"] for c in CONVS.get(token, [])]:
                return httpx.Response(404, json={"detail": "not found"})
            if req.method == "DELETE":
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"id": cid, "messages": [
                {"id": 41, "role": "user", "content": "how many leaves?", "meta": {}},
                {"id": 42, "role": "assistant", "content": "18 [1]", "meta": {"sources": [{"n": 1, "source": "HR.pdf", "page": None}]}}]})
        return httpx.Response(404)

    monkeypatch.setattr(backend, "transport", httpx.MockTransport(handle))
    monkeypatch.setattr(backend, "_tokens", {})
    monkeypatch.setattr(backend, "_owners", {})
    return a


def run(coro):
    return asyncio.run(coro)


def test_sign_in_goes_through_the_api_and_keeps_the_session_token(api):
    user = run(backend.sign_in("amy@sysfore.com", "right password 1"))
    assert user.identifier == "amy@sysfore.com" and user.metadata["token"] == "amy-token"
    assert run(backend.sign_in("amy@sysfore.com", "wrong")) is None


def test_a_question_carries_the_persons_token_and_the_thread_as_conversation(api):
    status, body = run(backend.ask("amy-token", "t1", "how many leaves?"))
    assert status == 200 and api.calls[-1] == ("POST", "/chat", "amy-token", {"message": "how many leaves?", "conv_id": "t1"})
    assert backend.with_sources(body["answer"], body["sources"]).endswith("**Sources**\n- [1] HR Policy.pdf, p.4")


def test_an_unreachable_api_is_reported_not_raised(monkeypatch):
    def down(req):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(backend, "transport", httpx.MockTransport(down))
    status, body = run(backend.ask("t", "c", "hi"))
    assert status == 0 and "Cannot reach" in body["detail"]
    assert run(backend.sign_in("a@b.com", "x")) is None


def test_history_lists_only_the_signed_in_persons_conversations(api):
    dl = backend.BackendDataLayer("Sysfore Assistant")
    run(backend.sign_in("amy@sysfore.com", "right password 1"))
    page = run(dl.list_threads(Pagination(first=20), ThreadFilter(userId="amy@sysfore.com")))
    assert [t["id"] for t in page.data] == ["t1", "t2"] and page.data[0]["name"] == "leave policy"
    assert api.calls[-1][:3] == ("GET", "/conversations", "amy-token")
    assert run(dl.get_thread_author("t1")) == "amy@sysfore.com"
    assert run(dl.get_thread_author("t9")) == ""                 # someone else's: Chainlit answers "not found"
    found = run(dl.list_threads(Pagination(first=20), ThreadFilter(userId="amy@sysfore.com", search="WFH")))
    assert [t["id"] for t in found.data] == ["t2"]


def test_history_pages_by_cursor(api):
    dl = backend.BackendDataLayer()
    run(backend.sign_in("amy@sysfore.com", "right password 1"))
    first = run(dl.list_threads(Pagination(first=1), ThreadFilter(userId="amy@sysfore.com")))
    assert [t["id"] for t in first.data] == ["t1"] and first.pageInfo.hasNextPage
    rest = run(dl.list_threads(Pagination(first=1, cursor="t1"), ThreadFilter(userId="amy@sysfore.com")))
    assert [t["id"] for t in rest.data] == ["t2"] and not rest.pageInfo.hasNextPage


def test_an_opened_conversation_shows_both_sides_with_sources(api):
    dl = backend.BackendDataLayer("Sysfore Assistant")
    run(backend.sign_in("amy@sysfore.com", "right password 1"))
    run(dl.list_threads(Pagination(first=20), ThreadFilter(userId="amy@sysfore.com")))
    thread = run(dl.get_thread("t1"))
    assert thread["userIdentifier"] == "amy@sysfore.com"
    assert [(s["type"], s["name"]) for s in thread["steps"]] == [("user_message", "amy@sysfore.com"),
                                                                 ("assistant_message", "Sysfore Assistant")]
    assert thread["steps"][1]["output"] == "18 [1]\n\n**Sources**\n- [1] HR.pdf"
    assert [s["id"] for s in thread["steps"]] == ["msg-41", "msg-42"]          # the API's ids, so a rating can name the answer
    assert run(dl.get_thread("never-listed")) is None


def test_a_restart_recovers_the_token_from_the_signed_cookie(api):
    dl = backend.BackendDataLayer()
    from chainlit.user import User
    assert run(dl.get_user("amy@sysfore.com")) is None           # nothing in memory after a restart
    persisted = run(dl.create_user(User(identifier="amy@sysfore.com", metadata={"token": "amy-token"})))
    assert persisted.metadata == {}                              # Chainlit shows this user to the page's scripts
    assert backend.token_for(persisted) == "amy-token" and run(dl.get_user("amy@sysfore.com")) is not None
    assert run(dl.create_user(User(identifier="x@sysfore.com", metadata={}))) is None


def test_a_new_chat_belongs_to_its_author_and_can_be_deleted(api):
    dl = backend.BackendDataLayer()
    run(backend.sign_in("amy@sysfore.com", "right password 1"))
    run(dl.update_thread("t1", user_id="amy@sysfore.com"))
    assert run(dl.get_thread_author("t1")) == "amy@sysfore.com"
    run(dl.delete_thread("t1"))
    assert api.calls[-1][:3] == ("DELETE", "/conversations/t1", "amy-token") and run(dl.get_thread_author("t1")) == ""


def test_feedback_is_logged_without_the_comment(api, caplog):
    dl = backend.BackendDataLayer()
    with caplog.at_level("INFO", logger="webchat"):
        run(dl.upsert_feedback(Feedback(forId="msg-42", value=0, threadId="t1", comment="call me on 98450 12345")))
    assert "msg-42" in caplog.text and "with a comment" in caplog.text
    assert "98450" not in caplog.text                              # free text can hold personal data


@pytest.fixture
def page(api):
    from starlette.testclient import TestClient
    import chat as webchat_app
    return TestClient(webchat_app.server)


def test_the_set_password_page_comes_before_chainlits_own_pages(page):
    r = page.get("/set-password?token=a\"<b")
    assert r.status_code == 200 and "Choose your password" in r.text
    assert 'value="a&quot;&lt;b"' in r.text and r.headers["referrer-policy"] == "no-referrer"
    assert page.get("/set-password").status_code == 400


def test_choosing_a_password(page, api):
    mismatch = page.post("/set-password", data={"token": "good-link", "password": "a" * 12, "again": "b" * 12})
    assert mismatch.status_code == 400 and "do not match" in mismatch.text
    assert not [c for c in api.calls if c[1] == "/chat-auth/set-password"]
    expired = page.post("/set-password", data={"token": "old", "password": "a" * 12, "again": "a" * 12})
    assert expired.status_code == 400 and "This link has expired." in expired.text
    ok = page.post("/set-password", data={"token": "good-link", "password": "a" * 12, "again": "a" * 12})
    assert ok.status_code == 200 and "Password saved" in ok.text and 'href="/login"' in ok.text
