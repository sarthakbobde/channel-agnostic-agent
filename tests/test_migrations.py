"""Alembic migrations: fresh install, in-place upgrade of an existing database, down and up."""
import sqlite3

import pytest
from sqlalchemy import inspect, text

from app.admin import users
from app.channels.manager import ChannelManager
from app.channels.store import ChannelStore
from app.db import Database
from app.secretstore import FernetSecretStore, generate_key
from tests.conftest import TEST_DB

HEAD = "0009"
TABLES = {"meta", "channels", "secrets", "users", "departments", "department_editors", "set_password_tokens", "knowledge_bases", "documents", "kb_index_versions", "audit_log", "global_settings", "job_locks",
          "alembic_version", "llm_providers", "model_settings", "model_prices", "usage_records", "alerts"}

# The schema exactly as it was before migrations existed (captured from the live database).
LEGACY_DDL = """
CREATE TABLE admin_users (id INTEGER NOT NULL, email VARCHAR(254) NOT NULL, role VARCHAR(16) NOT NULL,
  provider VARCHAR(16) NOT NULL, password_hash TEXT, created_at DATETIME NOT NULL, PRIMARY KEY (id), UNIQUE (email));
CREATE TABLE audit_log (id INTEGER NOT NULL, ts DATETIME NOT NULL, actor VARCHAR(254) NOT NULL,
  action VARCHAR(64) NOT NULL, target VARCHAR(64) NOT NULL, detail TEXT NOT NULL, PRIMARY KEY (id));
CREATE INDEX ix_audit_log_ts ON audit_log (ts);
CREATE TABLE channels (id INTEGER NOT NULL, type VARCHAR(32) NOT NULL, enabled BOOLEAN NOT NULL, config JSON NOT NULL,
  updated_at DATETIME NOT NULL, PRIMARY KEY (id), UNIQUE (type));
CREATE TABLE meta ("key" VARCHAR(64) NOT NULL, value TEXT NOT NULL, PRIMARY KEY ("key"));
CREATE TABLE secrets (id INTEGER NOT NULL, channel_type VARCHAR(32) NOT NULL, name VARCHAR(64) NOT NULL,
  ciphertext TEXT NOT NULL, updated_at DATETIME NOT NULL, PRIMARY KEY (id), UNIQUE (channel_type, name));
"""


def revision(db: Database) -> str:
    with db.engine.connect() as c:
        return c.execute(text("SELECT version_num FROM alembic_version")).scalar()


def tables(db: Database) -> set[str]:
    return set(inspect(db.engine).get_table_names())


def columns(db: Database, table: str) -> set[str]:
    return {c["name"] for c in inspect(db.engine).get_columns(table)}


def test_fresh_database_is_at_head_with_all_tables(new_db):
    db = new_db()
    assert revision(db) == HEAD and tables(db) == TABLES
    assert columns(db, "secrets") == {"id", "owner_type", "owner_id", "name", "ciphertext", "updated_at"}


def test_running_migrations_twice_is_a_no_op(new_db):
    db = new_db()
    db.migrate()
    db.migrate()
    assert revision(db) == HEAD


def test_downgrade_to_base_and_back_up(new_db):
    db = new_db()
    db.downgrade("base")
    assert tables(db) <= {"alembic_version"}
    db.migrate()
    assert revision(db) == HEAD and tables(db) == TABLES


def test_downgrade_one_step_restores_the_old_secrets_shape_and_keeps_channel_secrets(new_db):
    key = generate_key()
    mgr = ChannelManager()
    db = new_db()
    store = ChannelStore(db, FernetSecretStore(key), lambda t: mgr.adapter_class(t).SPEC)
    store.save("slack", enabled=True, values={"bot_token": "xoxb-1", "app_token": "xapp-1"}, actor="a")
    db.downgrade("0001")
    assert columns(db, "secrets") == {"id", "channel_type", "name", "ciphertext", "updated_at"}
    with db.engine.connect() as c:
        rows = c.execute(text("SELECT channel_type, name FROM secrets ORDER BY name")).all()
    assert [tuple(r) for r in rows] == [("slack", "app_token"), ("slack", "bot_token")]
    db.migrate()
    assert store.runtime_config("slack")[1]["bot_token"] == "xoxb-1"        # secrets survive the round trip


@pytest.mark.skipif(TEST_DB != "sqlite", reason="legacy databases were SQLite")
def test_database_created_before_migrations_upgrades_in_place_with_data_intact(tmp_path):
    key = generate_key()
    secrets = FernetSecretStore(key)
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(LEGACY_DDL)
    now = "2026-10-12 11:00:00.000000"
    from app.admin.auth import hash_password
    pw_hash = hash_password("a long legacy password")
    bot, app_tok = secrets.encrypt("xoxb-legacy-bot"), secrets.encrypt("xapp-legacy-app")
    legacy.executemany("INSERT INTO meta VALUES (?, ?)", [("schema_version", "1"), ("config_imported", "1")])
    legacy.execute("INSERT INTO channels VALUES (1, 'slack', 1, '{\"show_sources\": true}', ?)", (now,))
    legacy.execute("INSERT INTO channels VALUES (2, 'teams', 0, '{}', ?)", (now,))
    legacy.executemany("INSERT INTO secrets VALUES (?, 'slack', ?, ?, ?)",
                       [(1, "bot_token", bot, now), (2, "app_token", app_tok, now)])
    legacy.execute("INSERT INTO admin_users VALUES (1, 'boss@sysfore.com', 'admin', 'local', ?, ?)", (pw_hash, now))
    legacy.execute("INSERT INTO audit_log VALUES (1, ?, 'system', 'import', '', 'Imported from channels.yaml')", (now,))
    legacy.commit()
    legacy.close()

    db = Database(path)                                                    # <- the upgrade

    assert revision(db) == HEAD and tables(db) == TABLES
    mgr = ChannelManager()
    store = ChannelStore(db, secrets, lambda t: mgr.adapter_class(t).SPEC)
    enabled, cfg = store.runtime_config("slack")
    assert enabled is True and cfg["bot_token"] == "xoxb-legacy-bot" and cfg["app_token"] == "xapp-legacy-app"
    assert store.types() == ["slack", "teams"]
    assert users.authenticate(db, "boss@sysfore.com", "a long legacy password")["email"] == "boss@sysfore.com"
    assert [e["action"] for e in store.recent_audit()] == ["import"]
    with db.engine.connect() as c:                                         # ciphertext copied byte for byte
        stored = dict(c.execute(text("SELECT name, ciphertext FROM secrets WHERE owner_type = 'channel'")).all())
        meta = dict(c.execute(text("SELECT key, value FROM meta")).all())
    assert stored == {"bot_token": bot, "app_token": app_tok}
    assert meta["config_imported"] == "1"
    # new writes after the upgrade work (ids continue, nothing collides)
    store.save("slack", enabled=True, values={"bot_token": "xoxb-new"}, actor="a")
    store.save("gchat", enabled=False, values={"audience": "123"}, actor="a")
    assert store.runtime_config("slack")[1]["bot_token"] == "xoxb-new"
    assert store.runtime_config("slack")[1]["app_token"] == "xapp-legacy-app"
