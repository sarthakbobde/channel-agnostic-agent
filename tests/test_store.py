"""Secret store and channel store tests: encryption, masking, audit, YAML import."""
import glob

import pytest

from app.channels.manager import ChannelManager
from app.channels.store import ChannelStore
from app.secretstore import FernetSecretStore, SecretError, generate_key

TOKEN = "xoxb-VERY-SECRET-1234567890"
APP_TOKEN = "xapp-ANOTHER-SECRET-0987654321"


@pytest.fixture
def key():
    return generate_key()


@pytest.fixture
def store(new_db, key):
    mgr = ChannelManager()
    return ChannelStore(new_db(), FernetSecretStore(key), lambda t: mgr.adapter_class(t).SPEC)


def _db_bytes(tmp_path) -> bytes:
    """Raw SQLite files (including the write-ahead log); empty on other backends."""
    return b"".join(open(p, "rb").read() for p in glob.glob(str(tmp_path / "admin.db*")))


# ---- secret store -----------------------------------------------------------
def test_secret_roundtrip_and_not_plaintext(key):
    s = FernetSecretStore(key)
    tok = s.encrypt("hunter2")
    assert "hunter2" not in tok and s.decrypt(tok) == "hunter2"


def test_missing_or_invalid_key_is_a_clear_error():
    with pytest.raises(SecretError, match="APP_SECRET_KEY is not set"):
        FernetSecretStore("")
    with pytest.raises(SecretError, match="not a valid key"):
        FernetSecretStore("not-a-key")


def test_wrong_key_cannot_decrypt(key):
    tok = FernetSecretStore(key).encrypt("x")
    with pytest.raises(SecretError, match="master key"):
        FernetSecretStore(generate_key()).decrypt(tok)


def test_key_rotation(key):
    old = FernetSecretStore(key)
    tok = old.encrypt("x")
    new_key = generate_key()
    both = FernetSecretStore(f"{new_key},{key}")              # new primary, old still readable
    assert both.decrypt(tok) == "x"
    rotated = both.rotate(tok)
    assert FernetSecretStore(new_key).decrypt(rotated) == "x"  # old key no longer needed


# ---- channel store ----------------------------------------------------------
def test_secrets_are_encrypted_at_rest(store, tmp_path, db_text):
    store.save("slack", enabled=True, values={"bot_token": TOKEN, "app_token": APP_TOKEN,
                                              "show_sources": True}, actor="admin@x.com")
    raw = _db_bytes(tmp_path)
    assert TOKEN.encode() not in raw and APP_TOKEN.encode() not in raw
    dump = db_text(store.db)
    assert TOKEN not in dump and APP_TOKEN not in dump and "gAAAA" in dump          # ciphertext is there
    enabled, cfg = store.runtime_config("slack")
    assert enabled and cfg["bot_token"] == TOKEN and cfg["app_token"] == APP_TOKEN
    assert cfg["show_sources"] is True


def test_public_view_never_exposes_secrets(store):
    store.save("slack", enabled=True, values={"bot_token": TOKEN, "allowed_channels": ["C1"]}, actor="a")
    view = store.public_view("slack")
    assert TOKEN not in repr(view)
    by = {f["name"]: f for f in view["fields"]}
    assert by["bot_token"]["is_set"] is True and "value" not in by["bot_token"]
    assert by["app_token"]["is_set"] is False
    assert by["allowed_channels"]["value"] == ["C1"]


def test_empty_secret_keeps_existing_and_new_value_replaces(store):
    store.save("slack", enabled=True, values={"bot_token": TOKEN, "app_token": APP_TOKEN}, actor="a")
    store.save("slack", enabled=True, values={"bot_token": "", "show_sources": False}, actor="a")
    _, cfg = store.runtime_config("slack")
    assert cfg["bot_token"] == TOKEN and cfg["show_sources"] is False
    store.save("slack", enabled=True, values={"bot_token": "xoxb-new"}, actor="a")
    assert store.runtime_config("slack")[1]["bot_token"] == "xoxb-new"


def test_unknown_setting_and_type_rejected(store):
    with pytest.raises(ValueError, match="unknown settings"):
        store.save("slack", enabled=True, values={"nope": 1}, actor="a")
    with pytest.raises(ValueError, match="unknown channel type"):
        store.save("carrier-pigeon", enabled=True, values={}, actor="a")


def test_audit_trail_has_no_secret_values(store, tmp_path):
    store.save("slack", enabled=False, values={"bot_token": TOKEN, "app_token": APP_TOKEN}, actor="a@x.com")
    store.save("slack", enabled=True, values={}, actor="a@x.com")
    log = store.recent_audit()
    actions = [e["action"] for e in log]
    assert "channel.create" in actions and "secret.replace" in actions and "channel.enable" in actions
    assert all(TOKEN not in str(e) and APP_TOKEN not in str(e) for e in log)
    assert all(e["actor"] == "a@x.com" for e in log)


def test_delete_removes_channel_and_secrets(store, tmp_path):
    store.save("slack", enabled=True, values={"bot_token": TOKEN}, actor="a")
    assert store.delete("slack", actor="a") is True
    assert store.runtime_config("slack") is None and store.types() == []
    assert store.delete("slack", actor="a") is False


def test_wrong_master_key_fails_loudly(store, tmp_path):
    store.save("slack", enabled=True, values={"bot_token": TOKEN}, actor="a")
    mgr = ChannelManager()
    other = ChannelStore(store.db, FernetSecretStore(generate_key()), lambda t: mgr.adapter_class(t).SPEC)
    with pytest.raises(SecretError):
        other.runtime_config("slack")


# ---- legacy import ----------------------------------------------------------
LEGACY = {"defaults": {"show_sources": True},
          "channels": {"slack": {"enabled": True, "bot_token": TOKEN, "app_token": APP_TOKEN,
                                 "allowed_channels": []},
                       "teams": {"enabled": False, "app_id": "", "app_password": "", "tenant_id": ""},
                       "mystery": {"enabled": True}}}


def test_import_once_seeds_database(store, tmp_path, db_text):
    assert store.import_config_once(LEGACY) == ["slack", "teams"]      # unknown type skipped
    enabled, cfg = store.runtime_config("slack")
    assert enabled and cfg["bot_token"] == TOKEN and cfg["show_sources"] is True
    t_enabled, t_cfg = store.runtime_config("teams")
    assert not t_enabled and "app_id" not in t_cfg                       # empty values not stored
    assert TOKEN.encode() not in _db_bytes(tmp_path) and TOKEN not in db_text(store.db)
    assert any(e["action"] == "import" and e["actor"] == "system" for e in store.recent_audit())


def test_import_runs_only_once(store):
    store.import_config_once(LEGACY)
    store.delete("slack", actor="admin")
    assert store.import_config_once(LEGACY) == []                        # delete is not undone
    assert store.types() == ["teams"]


# ---- manager reads from the store -------------------------------------------
def test_start_from_store_starts_only_enabled(store):
    from tests.test_channels import FakeAdapter  # reuse the fake adapter
    FakeAdapter.instances = []

    class M(ChannelManager):
        def adapter_class(self, typ):
            return FakeAdapter

    fake_store = ChannelStore(store.db, store.secrets, lambda t: FakeAdapter.SPEC)
    fake_store.save("on", enabled=True, values={"token": "t1"}, actor="a")
    fake_store.save("off", enabled=False, values={"token": "t2"}, actor="a")
    m = M({"on": ("x", "y"), "off": ("x", "y")})
    m.start_from_store(fake_store)
    assert m.running() == ["on"] and m.get("on").cfg["token"] == "t1"
