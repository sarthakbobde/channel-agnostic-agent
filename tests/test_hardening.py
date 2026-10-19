"""Phase 10c: the question rate limit, master-key backup and rotation, and a concurrency check that no turn is lost."""
import os
import subprocess
import sys
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import chat_service, global_settings, memory, ratelimit, rag, secretstore, summaries
from app.channels import core
from app.channels.base import Envelope
from app.db import Secret, get_db


# ---- the limiter ------------------------------------------------------------------------------------------------------
def test_the_window_slides_and_each_person_has_their_own():
    ratelimit.reset()
    assert [ratelimit.allow("a", 3, now=t)[0] for t in (0, 1, 2)] == [True, True, True]
    ok, wait = ratelimit.allow("a", 3, now=3)
    assert not ok and 55 <= wait <= 61
    assert ratelimit.allow("b", 3, now=3)[0]                    # someone else is not slowed down
    assert ratelimit.allow("a", 3, now=60.5)[0]                 # the first question has left the window


def test_a_limit_of_zero_switches_it_off():
    ratelimit.reset()
    assert all(ratelimit.allow("a", 0, now=i)[0] for i in range(1000))


def test_quiet_people_are_forgotten():
    ratelimit.reset()
    ratelimit.allow("old", 5, now=0)
    ratelimit.allow("new", 5, now=500)
    assert "old" not in ratelimit._hits


@pytest.fixture
def limit():
    before = global_settings.current().chat_rate_per_minute

    def set_to(n):
        global_settings.update({"chat_rate_per_minute": n}, "tests")
        ratelimit.reset()
    yield set_to
    global_settings.update({"chat_rate_per_minute": before}, "tests")
    ratelimit.reset()


def test_the_setting_is_validated_and_defaults_to_twenty():
    assert global_settings.defaults().chat_rate_per_minute == 20
    with pytest.raises(ValueError):
        global_settings.validate({"chat_rate_per_minute": -1}, global_settings.defaults())
    with pytest.raises(ValueError):
        global_settings.validate({"chat_rate_per_minute": 2.5}, global_settings.defaults())


def test_chat_api_answers_429_with_retry_after(limit, monkeypatch):
    from app.api import app
    monkeypatch.setattr(chat_service, "chat_stream", lambda *a, **k: (iter(["fine"]), {"sources": [], "searched": False, "query": ""}))
    limit(3)
    c = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1))
    h = {"Authorization": "Bearer test-chat-token"}
    body = {"message": "hi", "conv_id": "c1"}
    assert [c.post("/chat", json=body, headers=h).status_code for _ in range(3)] == [200, 200, 200]
    r = c.post("/chat", json=body, headers=h)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1 and "too quickly" in r.json()["detail"]
    limit(0)
    assert c.post("/chat", json=body, headers=h).status_code == 200


def test_chat_platforms_get_a_friendly_reply(limit, monkeypatch):
    monkeypatch.setattr(chat_service, "chat", lambda *a, **k: {"answer": "real answer", "sources": [], "searched": False, "query": ""})
    limit(2)
    env = Envelope("slack", "T1", "C1", "t1", "U1", "hello", (), "asha@sysfore.com")
    other = Envelope("slack", "T1", "C1", "t1", "U2", "hello", (), "ravi@sysfore.com")
    assert [core.handle(env)["answer"] for _ in range(2)] == ["real answer", "real answer"]
    assert core.handle(env)["answer"] == core.SLOW_DOWN
    assert core.handle(other)["answer"] == "real answer"


# ---- master key: fingerprint, check, rotate -----------------------------------------------------------------------------
def make_secrets(db, store, n=4):
    with db.session() as s:
        for i in range(n):
            s.add(Secret(owner_type="channel", owner_id="slack", name=f"s{i}", ciphertext=store.encrypt(f"value-{i}")))


def test_rotation_re_encrypts_everything_with_the_new_key(new_db):
    db = new_db()
    old, new = secretstore.generate_key(), secretstore.generate_key()
    make_secrets(db, secretstore.FernetSecretStore(old))
    both = secretstore.FernetSecretStore(f"{new},{old}")
    assert secretstore.check_all(db, both) == {"total": 4, "undecryptable": [], "old_key": 4}
    assert secretstore.rotate_all(db, both) == {"total": 4, "rotated": 4}
    assert secretstore.check_all(db, both)["old_key"] == 0
    only_new = secretstore.FernetSecretStore(new)                # the old key can now be removed
    assert secretstore.check_all(db, only_new) == {"total": 4, "undecryptable": [], "old_key": 0}
    with db.session() as s:
        assert sorted(only_new.decrypt(r.ciphertext) for r in s.scalars(select(Secret))) == [f"value-{i}" for i in range(4)]


def test_rotation_changes_nothing_if_any_secret_cannot_be_decrypted(new_db):
    db = new_db()
    old, new, stranger = (secretstore.generate_key() for _ in range(3))
    make_secrets(db, secretstore.FernetSecretStore(old), 3)
    with db.session() as s:
        s.add(Secret(owner_type="channel", owner_id="slack", name="alien", ciphertext=secretstore.FernetSecretStore(stranger).encrypt("x")))
        before = {r.id: r.ciphertext for r in s.scalars(select(Secret))}
    both = secretstore.FernetSecretStore(f"{new},{old}")
    assert len(secretstore.check_all(db, both)["undecryptable"]) == 1
    with pytest.raises(secretstore.SecretError):
        secretstore.rotate_all(db, both)
    with db.session() as s:
        assert {r.id: r.ciphertext for r in s.scalars(select(Secret))} == before


def test_the_key_command_line(tmp_path):
    from app.db import Database
    old, new = secretstore.generate_key(), secretstore.generate_key()
    path = tmp_path / "admin.db"
    db = Database(path)
    make_secrets(db, secretstore.FernetSecretStore(old), 2)
    db.engine.dispose()
    env = {**os.environ, "ADMIN_DB_PATH": str(path), "DATABASE_URL": "", "APP_SECRET_KEY": f"{new},{old}"}

    def run(*a):
        return subprocess.run([sys.executable, "-m", "app.secretstore", *a], capture_output=True, text=True, env=env, timeout=120)
    fp = run("fingerprint")
    assert fp.returncode == 0 and secretstore.fingerprint(new) in fp.stdout and secretstore.fingerprint(old) in fp.stdout
    assert new not in fp.stdout and old not in fp.stdout                        # the keys themselves are never printed
    assert "2 still use an older key" in run("check").stdout
    out = run("rotate")
    assert out.returncode == 0 and "2 of 2" in out.stdout
    assert "0 still use an older key" in run("check").stdout
    wrong = subprocess.run([sys.executable, "-m", "app.secretstore", "rotate"], capture_output=True, text=True, timeout=120,
                           env={**env, "APP_SECRET_KEY": secretstore.generate_key()})
    assert wrong.returncode == 1 and "Nothing was changed" in wrong.stdout


# ---- concurrency: no turn is lost ---------------------------------------------------------------------------------------
@pytest.fixture
def quick_model(monkeypatch, limit):
    limit(0)

    def stream_direct(message, history, **kw):
        yield "answer to: "
        yield message
    monkeypatch.setattr(chat_service, "_decide", lambda *a, **k: {"search": False, "query": ""})
    monkeypatch.setattr(rag, "stream_direct", stream_direct)
    monkeypatch.setattr(summaries, "schedule", lambda *a, **k: None)      # the summariser is tested elsewhere; here no model is called


def test_parallel_conversations_lose_no_turns(quick_model):
    people, turns = 12, 5
    errors = []

    def person(i):
        try:
            for t in range(turns):
                r = core.handle(Envelope("slack", "T", f"load-{i}", "th", f"U{i}", f"q{i}-{t}", (), f"load{i}@sysfore.com"))
                assert r["answer"] == f"answer to: q{i}-{t}", r
        except Exception as e:                       # noqa: BLE001
            errors.append(repr(e))
    threads = [threading.Thread(target=person, args=(i,)) for i in range(people)]
    [t.start() for t in threads]
    [t.join(120) for t in threads]
    assert not errors, errors[:3]
    for i in range(people):
        msgs = memory.get_messages(f"slack-T-load-{i}-th")
        assert [m["content"] for m in msgs if m["role"] == "user"] == [f"q{i}-{t}" for t in range(turns)]
        assert [m["content"] for m in msgs if m["role"] == "assistant"] == [f"answer to: q{i}-{t}" for t in range(turns)]
        assert not [m for m in msgs if m.get("incomplete")]


def test_many_people_in_one_conversation_lose_no_turns(quick_model):
    n = 30
    conv = Envelope("slack", "T", "shared-room", "th", "U", "x", (), "")

    def ask(i):
        core.handle(Envelope("slack", "T", "shared-room", "th", f"U{i}", f"shared-{i}", (), f"s{i}@sysfore.com"))
    threads = [threading.Thread(target=ask, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join(120) for t in threads]
    msgs = memory.get_messages(conv.conv_id)
    assert sorted(m["content"] for m in msgs if m["role"] == "user") == sorted(f"shared-{i}" for i in range(n))
    assert len([m for m in msgs if m["role"] == "assistant"]) == n
