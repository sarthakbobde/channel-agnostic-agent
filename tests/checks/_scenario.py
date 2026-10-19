"""Shared set-up for the end-to-end scenarios (scenario_*.py).

`boot()` points the application at throwaway data folders BEFORE it is imported, builds the full admin application
(every router the real server mounts) behind a test client, and replaces only the outside world:
  * the language models: scripted stand-ins reached through the real provider settings and the real fallback chain;
  * the provider key test (no network);
  * nothing else. Documents are really uploaded, split, embedded (the built-in local model), stored and searched.
"""
import json
import os
import sys
import tempfile
import time


class Msg:
    def __init__(self, text, tin=100, tout=20):
        self.content, self.usage_metadata = text, {"input_tokens": tin, "output_tokens": tout}


class HTTPError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status_code = status


class Model:
    """A scripted chat model. `fail` (an exception or None) makes every call fail; `answer` is the text it streams.
    `seen` keeps everything it was sent, so a scenario can prove what a model did and did not receive."""

    def __init__(self, name):
        self.name, self.fail, self.answer, self.seen, self.calls = name, None, "Annual leave is 18 days [1].", [], 0

    def _record(self, messages):
        self.calls += 1
        self.seen.append("\n".join(str(m.content) for m in messages))
        if self.fail is not None:
            raise self.fail

    def invoke(self, messages, **kw):
        self._record(messages)
        q = str(messages[-1].content).split("Latest message:", 1)[-1].strip()
        return Msg(json.dumps({"search": True, "query": q}), 50, 10)

    def stream(self, messages, **kw):
        self._record(messages)
        yield Msg(self.answer)


class World:
    pass


def boot(guardrails: bool = False, remote: bool = False):
    sys.path.insert(0, os.getcwd())
    tmp = tempfile.mkdtemp()
    from cryptography.fernet import Fernet
    os.environ.update(ADMIN_DB_PATH=os.path.join(tmp, "admin.db"), CHAT_DIR=os.path.join(tmp, "chats"), CHROMA_PATH=os.path.join(tmp, "chroma"),
                      DOCS_DIR=os.path.join(tmp, "files"), DATABASE_URL="", GUARDRAILS_ENABLED="true" if guardrails else "false",
                      APP_SECRET_KEY=Fernet.generate_key().decode(), CHAT_API_TOKEN="svc-token",
                      ADMIN_ALLOW_REMOTE="true" if remote else "false", ADMIN_REQUIRE_HTTPS="true")
    w = World()
    w.tmp = tmp
    w.passed = w.failed = 0

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg, flush=True)
    w.ok = ok

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import app.ratelimit as ratelimit
    ratelimit.allow = lambda key, limit, now=None: (True, 0)         # the question limit is tested in tests/test_hardening.py
    from app import llm
    from app.admin import api as A, providers_api, users
    from app.admin.server import ROUTERS
    from app.channels import get_store
    from app.channels.manager import manager
    from app.db import get_db
    from app.knowledge import kbs, documents
    from app.providers import adapters

    w.db, w.store, w.manager, w.llm, w.users, w.kbs, w.documents = get_db(), get_store(), manager, llm, users, kbs, documents
    w.providers_api, w.A = providers_api, A
    kbs.ensure_default(w.db)

    w.models = {"openai": Model("openai"), "anthropic": Model("anthropic")}
    adapters.OpenAIProvider.build = lambda self, model, timeout: w.models["openai"]
    adapters.AnthropicProvider.build = lambda self, model, timeout: w.models["anthropic"]
    providers_api._check = lambda type_, cfg: ["model-a", "model-b"]

    from app.channels.slack import SlackAdapter

    class FakeSlack(SlackAdapter):
        """The real Slack settings form and rules, but no connection to Slack. A bot token of 'xoxb-bad' fails to start."""
        def __init__(self, cfg):
            self.cfg = cfg
            self.up = False

        def start(self):
            if self.cfg.get("bot_token") == "xoxb-bad":
                raise RuntimeError("invalid_auth")
            self.up = True

        def stop(self):
            self.up = False

        def healthy(self):
            return self.up

        def test(self):
            return (self.cfg.get("bot_token") != "xoxb-bad", "stand-in test")
    globals()["FakeSlack"] = FakeSlack
    manager.registry["slack"] = ("_scenario", "FakeSlack")

    app = FastAPI()
    for r in ROUTERS:
        app.include_router(r)
    w.app = app

    def client(host="localhost", **headers):
        return TestClient(app, base_url=f"{'https' if remote else 'http'}://{host}", client=("127.0.0.1", 50000), headers=headers)
    w.client = client

    def login(email, password, **kw):
        c = client(**kw)
        r = c.post("/admin/api/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, (email, r.status_code, r.text)
        return c, {"X-CSRF-Token": r.json()["csrf"]}
    w.login = login

    def person(email, role, department_id=None, password="a good password 1"):
        u = users.create_user(w.db, email, email.split("@")[0], role, department_id)
        t, _ = users.issue_set_password_link(w.db, u["id"], "setup")
        users.redeem_set_password_link(w.db, t, password)
        return u
    w.person = person

    def wait_doc(doc_id, secs=90):
        end = time.time() + secs
        while time.time() < end:
            d = documents.get(w.db, doc_id)
            if d["status"] in ("Indexed", "Failed"):
                return d
            time.sleep(0.2)
        return documents.get(w.db, doc_id)
    w.wait_doc = wait_doc

    def ask(email, text, kb_ids=(), channel="slack"):
        from app.channels import core
        from app.channels.base import Envelope
        return core.handle(Envelope(channel, "T1", "room", f"th-{time.time_ns()}", "U1", text, tuple(kb_ids), email))
    w.ask = ask
    return w
