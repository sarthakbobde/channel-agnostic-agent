"""Regression tests for the problems found in the QA round: sources, provider refusals, passage size with headings,
live passage counts and wording."""
import dataclasses

import pytest

from app import api as chat_api
from app import chat_service
from app.knowledge import chunkers, kbs, parsers, versions

SOURCES = [{"n": 1, "source": "leave.txt"}, {"n": 2, "source": "expense.txt"}, {"n": 3, "source": "it.txt"}]


def _src():
    return [dict(s) for s in SOURCES]


def test_a_could_not_find_reply_lists_no_sources():
    s = _src()
    chat_service.trim_sources("I couldn't find this in the knowledge base.", s)
    assert s == []


def test_only_cited_sources_are_kept():
    s = _src()
    chat_service.trim_sources("Employees get 18 days [1]. Hotel limit is 120 USD [3].", s)
    assert [x["n"] for x in s] == [1, 3]
    s = _src()
    chat_service.trim_sources("Both apply [1, 2].", s)
    assert [x["n"] for x in s] == [1, 2]


def test_an_answer_without_citations_keeps_every_source():
    s = _src()
    chat_service.trim_sources("Employees get 18 days.", s)
    assert len(s) == 3


def test_the_providers_own_content_filter_is_recognised():
    azure = Exception("Error code: 400 - {'error': {'code': 'content_filter', 'message': 'The response was filtered due to the prompt triggering Azure OpenAI's content management policy'}}")
    assert chat_api._refused_by_provider(azure)
    assert not chat_api._refused_by_provider(Exception("connection reset"))


def test_the_chat_endpoint_answers_politely_when_the_provider_refuses(monkeypatch):
    from fastapi.testclient import TestClient
    def boom(*a, **k):
        raise Exception("Error 400 content_filter jailbreak detected")
    monkeypatch.setattr(chat_api.chat_service, "chat_stream", boom)
    c = TestClient(chat_api.app)
    r = c.post("/chat", headers={"Authorization": "Bearer test-chat-token"}, json={"message": "hello", "conv_id": "t1"})
    assert r.status_code == 200
    assert r.json()["answer"] == chat_api.PROVIDER_REFUSED_REPLY and r.json()["sources"] == []


def test_a_passage_with_its_heading_path_never_exceeds_the_size(tmp_path):
    body = "Details are reviewed every year by the people team and the department heads. " * 20
    f = tmp_path / "policy.md"
    f.write_text("# Policies\n\n## Parking\n" + body + "\n\n## Pets\n" + body + "\n", encoding="utf-8")
    chunks = chunkers.build("by_heading", 500, 50).chunk(f)
    assert len(chunks) >= 4
    assert max(len(c.text) for c in chunks) <= 500, [len(c.text) for c in chunks]


def test_short_sections_are_not_all_merged_into_one_passage(tmp_path):
    f = tmp_path / "handbook.md"
    f.write_text("# Handbook\n\n## Working hours\nCore hours are 10:00 to 16:00. Employees may start between 8:00 and 10:00. Lunch is 45 minutes.\n\n"
                 "## Remote work\nEmployees may work remotely up to 3 days per week with manager approval. A home-office allowance of 400 USD is paid once.\n\n"
                 "## Security\nPasswords must be at least 14 characters long. Report phishing emails to the security desk within one hour.\n\n"
                 "## Training\nEvery employee has a yearly training budget of 1500 USD. Courses must be approved by the manager first.\n", encoding="utf-8")
    chunks = chunkers.build("by_heading", 900, 100).chunk(f)
    assert len(chunks) >= 3, [c.heading_path for c in chunks]


def test_wording_uses_passage_not_chunk():
    with pytest.raises(ValueError, match="Overlap .* smaller than the passage size"):
        kbs.check_chunking(300, 400)
    with pytest.raises(ValueError, match="Passage size must be"):
        kbs.check_chunking(50, None)


def test_the_active_index_counts_documents_added_after_it_was_built():
    from app.db import Document, get_db
    db = get_db()
    kb = kbs.ensure_default(db)
    versions.init_for(db, kb, chunks=0)
    with db.session() as s:
        s.add(Document(kb_id=kb["id"], name="late.txt", doc_key="late", status="Indexed", chunks=4))
    active = [v for v in versions.list_versions(db, kb["id"]) if v["is_active"]][0]
    assert active["chunks"] >= 4 and active["documents"] >= 1
