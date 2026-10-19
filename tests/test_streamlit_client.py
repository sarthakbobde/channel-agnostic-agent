"""The Streamlit chat page asks people to sign in, sends their session token on every protected call,
and goes back to the sign-in page when the session ends."""
import pathlib

import pytest
import requests
from streamlit.testing.v1 import AppTest

APP = str(pathlib.Path(__file__).resolve().parents[1] / "streamlit_app.py")


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


@pytest.fixture
def backend(monkeypatch):
    """Fake backend that records every request; set .chat_status to simulate a refusal."""
    class Backend:
        calls = []
        chat_status = 200
    b = Backend()

    def get(url, headers=None, timeout=None, **kw):
        b.calls.append(("GET", url, headers))
        if url.endswith("/health"):
            return Resp(200, {"status": "ok"})
        if url.endswith("/conversations"):
            return Resp(200, [])
        return Resp(200, {"id": "x", "messages": []})

    def post(url, headers=None, json=None, timeout=None, **kw):
        b.calls.append(("POST", url, headers))
        if url.endswith("/chat-auth/login"):
            if json["password"] != "right password 1":
                return Resp(401, {"detail": "Invalid email or password."})
            return Resp(200, {"token": "sess-123", "email": json["email"], "name": "Amy", "role": "viewer"})
        return Resp(b.chat_status, {"answer": "18 earned leaves [1]", "sources": [], "searched": True, "query": "q"})

    def delete(url, headers=None, **kw):
        b.calls.append(("DELETE", url, headers))
        return Resp(200, {})

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests, "delete", delete)
    return b


def signed_in():
    at = AppTest.from_file(APP, default_timeout=10).run()
    at.text_input[0].set_value("amy@sysfore.com")
    at.text_input[1].set_value("right password 1")
    at.button[0].click().run()
    return at


def test_nothing_is_shown_before_sign_in(backend):
    at = AppTest.from_file(APP, default_timeout=10).run()
    assert not at.exception and not at.chat_input
    assert not [c for c in backend.calls if c[1].endswith(("/conversations", "/chat"))]


def test_wrong_password_stays_on_the_sign_in_page(backend):
    at = AppTest.from_file(APP, default_timeout=10).run()
    at.text_input[0].set_value("amy@sysfore.com")
    at.text_input[1].set_value("nope")
    at.button[0].click().run()
    assert not at.chat_input and any("Invalid" in e.value for e in at.error)


def test_session_token_is_sent_on_every_protected_call(backend):
    at = signed_in()
    assert not at.exception and at.chat_input
    at.chat_input[0].set_value("how many leaves?").run()
    assert not at.exception
    protected = [(m, u, h) for m, u, h in backend.calls
                 if not u.endswith(("/health", "/chat-auth/login"))]
    assert any(m == "POST" and u.endswith("/chat") for m, u, h in protected)
    assert any(u.endswith("/conversations") for m, u, h in protected)
    assert all(h == {"Authorization": "Bearer sess-123"} for m, u, h in protected), protected


@pytest.mark.parametrize("status", [401, 503])
def test_an_ended_session_returns_to_sign_in(backend, status):
    at = signed_in()
    backend.chat_status = status
    at.chat_input[0].set_value("hi").run()
    assert not at.chat_input and any("session ended" in i.value for i in at.info)
