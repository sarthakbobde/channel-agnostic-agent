"""Shared test fixtures.

Run the suite on either database:
    python -m pytest tests                      # SQLite (default)
    TEST_DB=postgres python -m pytest tests     # PostgreSQL in a throwaway Docker container
Set TEST_POSTGRES_URL to use an existing server instead of Docker, for example
    postgresql+psycopg://postgres:secret@localhost:5432/postgres
"""
import os
import tempfile

# Tests must never touch the real admin database, chats or vector store. These are set BEFORE
# the app is imported (app.config reads the environment once, and a developer's .env must not win).
_SANDBOX = tempfile.mkdtemp(prefix="caa-tests-")
os.environ["ADMIN_DB_PATH"] = os.path.join(_SANDBOX, "admin.db")
os.environ["CHAT_DIR"] = os.path.join(_SANDBOX, "chats")
os.environ["CHROMA_PATH"] = os.path.join(_SANDBOX, "chroma")
os.environ["DATABASE_URL"] = ""
os.environ["CHAT_API_TOKEN"] = "test-chat-token"
os.environ["SLACK_BOT_TOKEN"] = ""
os.environ["SLACK_APP_TOKEN"] = ""
os.environ.setdefault("NINEROUTER_API_KEY", "test")
os.environ.setdefault("LLM_MODEL", "test")
import subprocess
import time
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text

from app.db import Database

TEST_DB = os.getenv("TEST_DB", "sqlite").lower()
PG_IMAGE = "postgres:16-alpine"
PG_PORT = 55432
PG_PASSWORD = "caa-test"


def _admin_engine(url):
    return create_engine(url, isolation_level="AUTOCOMMIT")


@pytest.fixture(scope="session")
def postgres_url():
    """Base URL of a PostgreSQL server (only when TEST_DB=postgres)."""
    if TEST_DB != "postgres":
        yield None
        return
    url = os.getenv("TEST_POSTGRES_URL")
    started = None
    if not url:
        started = "caa-test-postgres"
        subprocess.run(["docker", "rm", "-f", started], capture_output=True)
        run = subprocess.run(["docker", "run", "-d", "--name", started, "-e", f"POSTGRES_PASSWORD={PG_PASSWORD}",
                              "-p", f"{PG_PORT}:5432", PG_IMAGE], capture_output=True, text=True)
        if run.returncode:
            pytest.exit("Could not start PostgreSQL in Docker (is Docker Desktop running?): " + run.stderr, 2)
        url = f"postgresql+psycopg://postgres:{PG_PASSWORD}@localhost:{PG_PORT}/postgres"
    engine = _admin_engine(url)
    for _ in range(60):
        try:
            with engine.connect() as c:
                c.execute(text("SELECT 1"))
            break
        except Exception:
            time.sleep(1)
    else:
        pytest.exit("PostgreSQL did not become ready", 2)
    engine.dispose()
    yield url
    if started:
        subprocess.run(["docker", "rm", "-f", started], capture_output=True)


@pytest.fixture
def new_db(tmp_path, postgres_url):
    """Factory returning a fresh, migrated Database on the selected backend."""
    made = []

    def make(name: str = "admin") -> Database:
        if TEST_DB == "postgres":
            dbname = "t_" + uuid.uuid4().hex[:12]
            admin = _admin_engine(postgres_url)
            with admin.connect() as c:
                c.execute(text(f'CREATE DATABASE "{dbname}"'))
            admin.dispose()
            db = Database(postgres_url.rsplit("/", 1)[0] + "/" + dbname)
        else:
            db = Database(tmp_path / f"{name}.db")
        made.append(db)
        return db

    yield make
    for db in made:
        db.engine.dispose()
        if TEST_DB == "postgres":
            admin = _admin_engine(postgres_url)
            with admin.connect() as c:
                c.execute(text(f'DROP DATABASE IF EXISTS "{db.engine.url.database}" WITH (FORCE)'))
            admin.dispose()


@pytest.fixture
def db_text():
    """Everything stored in a database as one string (every table, every row), for
    'this secret is not stored in plaintext' assertions on any backend."""
    def dump(db: Database) -> str:
        out = []
        with db.engine.connect() as conn:
            for table in inspect(conn).get_table_names():
                for row in conn.execute(text(f'SELECT * FROM "{table}"')):
                    out.append(str(tuple(row)))
        return "\n".join(out)
    return dump
