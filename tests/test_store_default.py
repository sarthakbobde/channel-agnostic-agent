"""Which vector store a new knowledge base starts with: pgvector when the admin database is PostgreSQL with pgvector
available (the index then survives a container whose disk is temporary), Chroma otherwise. The pgvector check is
patched here; the real one is covered by test_store_contract against a live server."""
import dataclasses

import pytest

from app import retriever
from app.config import settings
from app.db import Department
from app.knowledge import kbs, storeadapters, versions


@pytest.fixture
def prefer(monkeypatch):
    """prefer(url, available): pretend DATABASE_URL is `url` and pgvector is (not) available on it."""
    calls = []

    def setup(url: str, available: bool = True):
        monkeypatch.setattr(storeadapters, "_preferred", None)
        monkeypatch.setattr("app.config.settings", dataclasses.replace(settings, database_url=url))
        monkeypatch.setattr(storeadapters.PgVectorStore, "test",
                            classmethod(lambda cls, config, secrets: calls.append(1) or (available, "patched")))
        return calls
    yield setup
    storeadapters._preferred = None


def _dept(db):
    with db.session() as s:
        return s.query(Department.id).filter(Department.is_default.is_(True)).scalar()


def test_sqlite_keeps_chroma(new_db, prefer):
    calls = prefer("")
    db = new_db()
    kb = kbs.create_kb(db, "HR", "", _dept(db))
    assert kb["store"] == "chroma" and calls == []          # no PostgreSQL, so nothing is probed


def test_postgres_with_pgvector_starts_new_knowledge_bases_on_pgvector(new_db, prefer):
    calls = prefer("postgresql+psycopg://u:p@/caa?host=/cloudsql/x")
    db = new_db()
    kb = kbs.create_kb(db, "HR", "", _dept(db))
    assert kb["store"] == "pgvector" and kb["store_config"] == {}       # empty config = the admin database
    assert [v["store"] for v in versions.list_versions(db, kb["id"])] == ["pgvector"]
    kbs.create_kb(db, "Finance", "", _dept(db))
    assert len(calls) == 1                                  # checked once per process


def test_postgres_without_pgvector_falls_back_to_chroma(new_db, prefer):
    prefer("postgresql://u:p@localhost/caa", available=False)
    db = new_db()
    assert kbs.create_kb(db, "HR", "", _dept(db))["store"] == "chroma"


def test_an_explicit_store_wins(new_db, prefer):
    prefer("postgresql://u:p@localhost/caa")
    db = new_db()
    assert kbs.create_kb(db, "HR", "", _dept(db), store="chroma")["store"] == "chroma"


class _Collection:
    def __init__(self, n):
        self.n = n

    def count(self):
        return self.n


@pytest.mark.parametrize("chunks, expected", [(0, "pgvector"), (12, "chroma")])
def test_default_knowledge_base_moves_only_when_its_chroma_collection_is_empty(new_db, prefer, monkeypatch, chunks, expected):
    prefer("postgresql://u:p@localhost/caa")
    monkeypatch.setattr(retriever, "get_collection", lambda: _Collection(chunks))
    db = new_db()
    assert kbs.ensure_default(db)["store"] == expected


def test_default_knowledge_base_stays_on_chroma_when_its_collection_cannot_be_read(new_db, prefer, monkeypatch):
    prefer("postgresql://u:p@localhost/caa")

    def broken():
        raise RuntimeError("chroma unavailable")
    monkeypatch.setattr(retriever, "get_collection", broken)
    db = new_db()
    assert kbs.ensure_default(db)["store"] == "chroma"
