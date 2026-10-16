"""Channel manager and adapter tests. No network, no real credentials."""
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.channels import gchat
from app.channels.base import Adapter, Field
from app.channels.manager import ChannelManager


# ---- fakes -----------------------------------------------------------------
class FakeAdapter(Adapter):
    name = "fake"
    title = "Fake"
    webhook_path = "/channels/fake/hook"
    SPEC = (Field("token", "Token", secret=True, required=True),
            Field("mode", "Mode", kind="choice", choices=("a", "b")))
    instances: list = []

    def __init__(self, cfg):
        super().__init__(cfg)
        self.started = self.stopped = False
        FakeAdapter.instances.append(self)

    def start(self):
        if self.cfg.get("explode"):
            raise RuntimeError("bad credential")
        self.started = True

    def stop(self):
        self.stopped = True

    async def handle_webhook(self, request: Request):
        return {"token": self.cfg["token"]}


class FakeManager(ChannelManager):
    def adapter_class(self, typ):
        if typ != "fake":
            raise KeyError(typ)
        return FakeAdapter


@pytest.fixture
def mgr():
    FakeAdapter.instances = []
    return FakeManager({"fake": ("x", "y")})


# ---- manager ---------------------------------------------------------------
def test_validate_reports_missing_and_bad_choice():
    assert FakeAdapter.validate({}) == ["Token is required"]
    assert FakeAdapter.validate({"token": "t", "mode": "z"}) == ["Mode must be one of: a, b"]
    assert FakeAdapter.validate({"token": "t", "mode": "a"}) == []


def test_apply_starts_and_replaces_old(mgr):
    mgr.apply("fake", {"token": "one"})
    first = mgr.get("fake")
    assert first.started and not first.stopped
    mgr.apply("fake", {"token": "two"})
    assert mgr.get("fake") is not first
    assert mgr.get("fake").started and first.stopped


def test_failed_start_keeps_working_channel(mgr):
    mgr.apply("fake", {"token": "good"})
    good = mgr.get("fake")
    with pytest.raises(RuntimeError):
        mgr.apply("fake", {"token": "bad", "explode": True})
    assert mgr.get("fake") is good and not good.stopped


def test_apply_rejects_invalid_config(mgr):
    with pytest.raises(ValueError, match="Token is required"):
        mgr.apply("fake", {})
    assert mgr.get("fake") is None


def test_remove_and_stop_all(mgr):
    mgr.apply("fake", {"token": "t"})
    a = mgr.get("fake")
    mgr.remove("fake")
    assert a.stopped and mgr.running() == []
    mgr.apply("fake", {"token": "t"})
    mgr.stop_all()
    assert mgr.running() == []


def test_test_connection_wraps_errors(mgr):
    ok, msg = mgr.test("fake", {"token": "t"})
    assert ok
    ok, msg = mgr.test("fake", {})
    assert not ok and "required" in msg


def test_webhook_route_hot_swaps(mgr):
    app = FastAPI()
    mgr.mount(app)
    c = TestClient(app)
    assert c.post("/channels/fake/hook").status_code == 404      # nothing running yet
    mgr.apply("fake", {"token": "one"})
    assert c.post("/channels/fake/hook").json() == {"token": "one"}
    mgr.apply("fake", {"token": "two"})                          # no remount needed
    assert c.post("/channels/fake/hook").json() == {"token": "two"}
    mgr.remove("fake")
    assert c.post("/channels/fake/hook").status_code == 404


def test_specs_lists_types(mgr):
    assert mgr.specs()["fake"][0] == "Fake"


# ---- real adapters through the real registry -------------------------------
@pytest.fixture
def real():
    m = ChannelManager()
    app = FastAPI()
    m.mount(app)
    return m, TestClient(app, raise_server_exceptions=False)


def test_real_types_expose_specs(real):
    m, _ = real
    specs = m.specs()
    assert set(specs) == {"slack", "teams", "gchat", "web"}
    assert any(f.secret for f in specs["slack"][1])


def test_teams_rejects_unauthenticated_and_forged(real):
    m, c = real
    m.apply("teams", {"app_id": "00000000-0000-0000-0000-000000000000", "app_password": "x",
                      "tenant_id": "11111111-1111-1111-1111-111111111111"})
    body = {"type": "message", "text": "hi", "serviceUrl": "http://x", "channelId": "msteams",
            "conversation": {"id": "c"}, "from": {"id": "u"}, "recipient": {"id": "b"}}
    assert c.post("/channels/teams/messages", json=body).status_code == 401
    assert c.post("/channels/teams/messages", json=body,
                  headers={"Authorization": "Bearer a.b.c"}).status_code == 401
    assert c.post("/channels/teams/messages", content="x",
                  headers={"Content-Type": "text/plain"}).status_code == 415
    assert c.post("/channels/gchat/events", json={}).status_code == 404   # gchat not enabled


def test_teams_connection_test_fails_with_fake_credentials():
    ok, msg = ChannelManager().test("teams", {"app_id": "x", "app_password": "y"})
    assert not ok and msg


def test_gchat_events(real, monkeypatch):
    m, c = real
    m.apply("gchat", {"auth": "project_number", "audience": "123456789", "allowed_spaces": ["spaces/OK"]})
    monkeypatch.setattr(m.get("gchat"), "_verify", lambda auth: None)
    monkeypatch.setattr(gchat, "handle", lambda env: {"answer": "**20 days**",
                        "sources": [{"source": "HR.pdf", "page": 4}]})
    classic = {"type": "MESSAGE", "user": {"name": "users/1"}, "space": {"name": "spaces/OK"},
               "message": {"text": "@Bot q", "argumentText": " q", "thread": {"name": "spaces/OK/threads/T"}}}
    r = c.post("/channels/gchat/events", json=classic)
    assert r.json() == {"text": "*20 days*\n\n_Sources: HR.pdf p.4_"}
    addon = {"chat": {"user": {"name": "u"}, "messagePayload": {"space": {"name": "spaces/OK"},
             "message": {"text": "q", "thread": {"name": "spaces/OK/threads/T"}}}}}
    assert "hostAppDataAction" in c.post("/channels/gchat/events", json=addon).json()
    assert c.post("/channels/gchat/events", json={**classic, "space": {"name": "spaces/NO"}}).json() == {}
    assert c.post("/channels/gchat/events", json={"type": "ADDED_TO_SPACE"}).json()["text"].startswith("Hi!")


def test_gchat_unauthorized_and_timeout(real, monkeypatch):
    m, c = real
    m.apply("gchat", {"auth": "project_number", "audience": "1"})
    classic = {"type": "MESSAGE", "space": {"name": "s"}, "message": {"argumentText": "q"}}
    assert c.post("/channels/gchat/events", json=classic).status_code == 401
    monkeypatch.setattr(m.get("gchat"), "_verify", lambda auth: None)
    monkeypatch.setattr(gchat, "TIMEOUT_S", 0.1)
    monkeypatch.setattr(gchat, "handle", lambda env: (time.sleep(0.6), {"answer": "late", "sources": []})[1])
    assert c.post("/channels/gchat/events", json=classic).json()["text"] == gchat.TIMEOUT_REPLY
