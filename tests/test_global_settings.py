"""Global settings: first-run seeding, validation, hot reload, and that real consumers use them."""
import dataclasses
import types

import pytest
from sqlalchemy import text

from app import global_settings as gs
from app import ingest, memory, retriever
from app.knowledge import chunkers
from app.config import settings as env
from app.db import get_db


@pytest.fixture(autouse=True)
def fresh_cache():
    gs.invalidate()
    yield
    gs.invalidate()


def test_first_read_seeds_the_row_from_env_defaults(new_db):
    db = new_db()
    with db.engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM global_settings")).scalar() == 0
    snap = gs.current(db)
    assert snap == gs.defaults() == gs.Snapshot(5, 0.35, 6, 900, 150, 365, 4000)
    with db.engine.connect() as c:
        row = c.execute(text("SELECT id, updated_by FROM global_settings")).one()
    assert tuple(row) == (1, "system (seeded from .env)")


def test_update_persists_and_applies_to_the_next_read(new_db):
    db = new_db()
    before, after = gs.update({"top_k": 9, "recent_turns": 3}, "admin@x.com", db)
    assert (before.top_k, after.top_k, after.recent_turns, after.score_threshold) == (5, 9, 3, 0.35)
    assert gs.current(db).top_k == 9                                     # cache was invalidated by the update
    assert gs.view(db)["updated_by"] == "admin@x.com"


def test_partial_update_keeps_other_values_and_empty_update_is_a_no_op(new_db):
    db = new_db()
    gs.update({"chunk_size": 1200}, "a", db)
    before, after = gs.update({}, "a", db)
    assert before == after and after.chunk_size == 1200 and after.top_k == 5


def test_change_is_visible_within_the_cache_window_even_if_made_elsewhere(new_db, monkeypatch):
    """Another process updating the row is seen after the short cache expires (no restart)."""
    db = new_db()
    assert gs.current(db).top_k == 5
    with db.session() as s:
        s.execute(text("UPDATE global_settings SET top_k = 11"))
    assert gs.current(db).top_k == 5                                     # still cached
    monkeypatch.setattr(gs, "CACHE_SECONDS", 0)
    assert gs.current(db).top_k == 11


def test_unreadable_database_falls_back_to_env_defaults(caplog):
    class Broken:
        def session(self):
            raise RuntimeError("database is down")
    snap = gs.current(Broken())
    assert snap == gs.defaults()


@pytest.mark.parametrize("values", [{"top_k": 0}, {"top_k": 51}, {"score_threshold": -0.1}, {"chunk_size": 99},
                                    {"chunk_overlap": 150, "chunk_size": 150}, {"top_k": "5"}, {"top_k": None},
                                    {"top_k": 5.5}, {"bogus": 1}])
def test_invalid_values_are_rejected_and_change_nothing(new_db, values):
    db = new_db()
    with pytest.raises(ValueError):
        gs.update(values, "a", db)
    assert gs.current(db) == gs.defaults()


def test_whole_floats_are_accepted_for_integer_settings(new_db):
    db = new_db()
    assert gs.update({"top_k": 7.0}, "a", db)[1].top_k == 7              # JSON often sends 7.0


# ---- the real consumers read the database, so a change needs no restart -----------------
@pytest.fixture
def default_db_settings():
    """The shared test database used by retriever/memory/ingest; restored after each test."""
    db = get_db()
    original = gs.current(db)
    yield db
    gs.update(dataclasses.asdict(original), "test-restore", db)


def test_retrieval_uses_top_k_and_threshold_from_settings(default_db_settings, monkeypatch, open_document):
    asked = {}

    class Col:
        def count(self):
            return 9

        def query(self, query_texts, n_results, where=None):
            asked["n"] = n_results
            return {"documents": [["a", "b"]], "metadatas": [[{"source": "s"}, {"source": "s"}]],
                    "distances": [[0.3, 0.45]]}                           # scores 0.7 and 0.55

    monkeypatch.setattr(retriever, "get_collection", lambda: Col())
    gs.update({"top_k": 3, "score_threshold": 0.6}, "t", default_db_settings)
    got = retriever.retrieve("q")
    assert asked["n"] == 3 and [c["text"] for c in got] == ["a"]         # 0.55 now falls below 0.6
    gs.update({"top_k": 5, "score_threshold": 0.35}, "t", default_db_settings)
    assert len(retriever.retrieve("q")) == 2 and asked["n"] == 5


def test_memory_window_follows_recent_turns(default_db_settings, tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "settings", dataclasses.replace(env, chat_dir=str(tmp_path)))
    for i in range(8):
        memory.add("c", "user", f"q{i}")
        memory.add("c", "assistant", f"a{i}")
    assert len(memory.get("c")) == 12                                    # 6 turns
    gs.update({"recent_turns": 2}, "t", default_db_settings)
    got = memory.get("c")
    assert len(got) == 4 and got[0] == ("user", "q6")


def test_indexing_uses_chunk_settings_from_the_database(default_db_settings, tmp_path, monkeypatch):
    seen = {}

    class Splitter:
        def __init__(self, chunk_size, chunk_overlap):
            seen.update(size=chunk_size, overlap=chunk_overlap)

        def split_text(self, t):
            return [t]

    class Col:
        def delete(self, **kw): pass
        def upsert(self, **kw): pass
        def count(self): return 0

    monkeypatch.setattr(chunkers, "RecursiveCharacterTextSplitter", Splitter)
    monkeypatch.setattr(retriever, "get_collection", lambda: Col())
    (tmp_path / "doc.txt").write_text("hello", encoding="utf-8")
    # indexing follows the ACTIVE index version's settings (a global change only takes effect through a rebuild)
    from app.db import KBIndexVersion
    from app.knowledge import kbs, versions
    kb = kbs.ensure_default(default_db_settings)
    versions.init_for(default_db_settings, kb)
    with default_db_settings.session() as s:
        v = s.query(KBIndexVersion).filter_by(kb_id=kb["id"], is_active=True).one()
        original = (v.chunk_size, v.chunk_overlap)
        v.chunk_size, v.chunk_overlap = 1200, 200
    try:
        ingest.ingest(str(tmp_path))
        assert seen == {"size": 1200, "overlap": 200}
    finally:
        with default_db_settings.session() as s:
            v = s.query(KBIndexVersion).filter_by(kb_id=kb["id"], is_active=True).one()
            v.chunk_size, v.chunk_overlap = original
