"""Characterization tests: lock in today's answering behaviour before any refactor.

They use fakes for the LLM and the vector store, so they need no network and no keys.
If one of these fails after a change, behaviour changed; decide deliberately before editing it.
"""
import dataclasses
import types

import pytest

from app import chat_service, memory, rag, retriever
from app.config import settings

FALLBACK = "I couldn't find this in the knowledge base."


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeModel:
    """Stands in for the chat model; records what it was asked."""

    def __init__(self, reply="", parts=None):
        self.reply, self.parts, self.calls = reply, parts or [], []

    def invoke(self, msgs, **kw):
        self.calls.append(("invoke", msgs))
        return FakeMessage(self.reply)

    def stream(self, msgs, **kw):
        self.calls.append(("stream", msgs))
        for p in self.parts:
            yield FakeMessage(p)


def chunk(text, source="HR-Policy.pdf", page=4, score=0.9):
    return {"text": text, "source": source, "page": page, "score": score}


# ---- retrieval: top-k 5, threshold 0.35 ----------------------------------------------
def test_defaults_are_top5_and_threshold_035():
    assert settings.top_k == 5 and settings.score_threshold == 0.35
    assert settings.memory_max_turns == 6
    assert (settings.chunk_size, settings.chunk_overlap) == (900, 150)


def test_threshold_is_inclusive_and_filters_below(monkeypatch):
    monkeypatch.setattr(retriever, "retrieve_scored",
                        lambda q, top_k=None: [chunk("a", score=0.9), chunk("b", score=0.35),
                                               chunk("c", score=0.349), chunk("d", score=0.1)])
    got = retriever.retrieve("anything")
    assert [c["text"] for c in got] == ["a", "b"]


def test_retrieve_scored_asks_chroma_for_five_and_converts_distance(monkeypatch, open_document):
    asked = {}

    class Col:
        def count(self):
            return 10

        def query(self, query_texts, n_results, where=None):
            asked["n"], asked["q"] = n_results, query_texts
            return {"documents": [["doc1", "doc2"]],
                    "metadatas": [[{"source": "a.pdf", "page": 3}, {"source": "b.md", "page": 0}]],
                    "distances": [[0.25, 0.9]]}

    monkeypatch.setattr(retriever, "get_collection", lambda: Col())
    got = retriever.retrieve_scored("leave policy")
    assert asked == {"n": 5, "q": ["leave policy"]}
    assert got[0] == {"text": "doc1", "source": "a.pdf", "page": 3, "score": 0.75}   # score = 1 - distance
    assert got[1]["page"] is None and got[1]["score"] == 0.1                          # page 0 means "no page"


def test_empty_collection_returns_nothing(monkeypatch):
    class Col:
        def count(self):
            return 0
    monkeypatch.setattr(retriever, "get_collection", lambda: Col())
    assert retriever.retrieve_scored("x") == []


# ---- grounded answer: [n] citations and the no-answer reply --------------------------
def test_no_chunks_gives_fixed_reply_and_never_calls_the_llm(monkeypatch):
    def boom():
        raise AssertionError("LLM must not be called when nothing was retrieved")
    monkeypatch.setattr(rag, "get_chat_model", boom)
    assert list(rag.stream_grounded("q", [])) == [FALLBACK]
    assert rag.FALLBACK == FALLBACK


def test_grounded_prompt_numbers_context_and_cites_file_and_page(monkeypatch):
    model = FakeModel(parts=["Employees get ", "18 days [1]."])
    monkeypatch.setattr(rag, "get_chat_model", lambda: model)
    out = "".join(rag.stream_grounded("How many leaves?", [chunk("Earned leave is 18 days.", page=4),
                                                         chunk("Casual leave is 12 days.", "HR.pdf", 5)]))
    assert out == "Employees get 18 days [1]."
    _, msgs = model.calls[0]
    system, human = msgs[0].content, msgs[1].content
    assert "[1] (HR-Policy.pdf, p.4)\nEarned leave is 18 days." in system
    assert "[2] (HR.pdf, p.5)\nCasual leave is 12 days." in system
    assert human == "How many leaves?"
    assert 'reply exactly: "I couldn\'t find this in the knowledge base."' in system
    assert "[1], [2]" in system and "ONLY the numbered context" in system


def test_page_zero_is_omitted_from_the_citation_label(monkeypatch):
    model = FakeModel(parts=["ok"])
    monkeypatch.setattr(rag, "get_chat_model", lambda: model)
    list(rag.stream_grounded("q", [chunk("text", "notes.md", page=None)]))
    assert "[1] (notes.md)\ntext" in model.calls[0][1][0].content


def test_format_sources_shape_and_snippet_limit():
    src = rag.format_sources([chunk("x" * 500, page=4, score=0.61)])
    assert src == [{"n": 1, "source": "HR-Policy.pdf", "page": 4, "score": 0.61, "snippet": "x" * 300}]


# ---- router: search or chat, standalone query ---------------------------------------
@pytest.mark.parametrize("raw, expect", [
    ('{"search": true, "query": "annual leave for contractors"}', (True, "annual leave for contractors")),
    ('{"search": false}', (False, "")),
    ('{"search": "false", "query": "ignored"}', (False, "")),
    ('Sure! {"search": true, "query": "diwali holiday"} done', (True, "diwali holiday")),
    ('{"search": true}', (True, "original message")),                  # missing query -> the message itself
    ("not json at all", (True, "original message")),                  # unparseable -> search (fail safe)
])
def test_router_decision(monkeypatch, raw, expect):
    monkeypatch.setattr(chat_service, "get_chat_model", lambda: FakeModel(reply=raw))
    d = chat_service._decide("original message", [("user", "hi"), ("assistant", "hello")])
    assert (d["search"], d["query"]) == expect


def test_router_sends_history_and_latest_message(monkeypatch):
    model = FakeModel(reply='{"search": false}')
    monkeypatch.setattr(chat_service, "get_chat_model", lambda: model)
    chat_service._decide("thanks", [("user", "hi"), ("assistant", "hello")])
    human = model.calls[0][1][1].content
    assert "user: hi\nassistant: hello" in human and "Latest message: thanks" in human


# ---- the whole turn: routing, retrieval, memory ------------------------------------------
@pytest.fixture
def chat_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "settings", dataclasses.replace(settings, chat_dir=str(tmp_path)))
    return tmp_path


def test_turn_with_search_saves_both_messages_only_after_streaming(monkeypatch, chat_dir):
    monkeypatch.setattr(chat_service, "get_chat_model", lambda: FakeModel(reply='{"search": true, "query": "leave"}'))
    monkeypatch.setattr(rag, "search", lambda q: [chunk("18 days", score=0.8)])
    monkeypatch.setattr(rag, "get_chat_model", lambda: FakeModel(parts=["18 ", "days [1]"]))
    tokens, meta = chat_service.chat_stream("how many leaves", "conv1")
    assert meta["searched"] is True and meta["query"] == "leave"
    assert meta["sources"][0]["source"] == "HR-Policy.pdf"
    assert memory.get("conv1") == []                                   # nothing saved yet
    assert "".join(tokens) == "18 days [1]"
    saved = memory.get("conv1")
    assert saved == [("user", "how many leaves"), ("assistant", "18 days [1]")]


def test_turn_without_search_uses_history_and_no_sources(monkeypatch, chat_dir):
    memory.add("c2", "user", "hello")
    memory.add("c2", "assistant", "hi there")
    router = FakeModel(reply='{"search": false}')
    answerer = FakeModel(parts=["You're welcome!"])
    monkeypatch.setattr(chat_service, "get_chat_model", lambda: router)
    monkeypatch.setattr(rag, "get_chat_model", lambda: answerer)
    monkeypatch.setattr(rag, "search", lambda q: pytest.fail("must not search"))
    out = chat_service.chat("thanks", "c2")
    assert out["answer"] == "You're welcome!" and out["searched"] is False and out["sources"] == []
    msgs = answerer.calls[0][1]
    assert [type(m).__name__ for m in msgs] == ["SystemMessage", "HumanMessage", "AIMessage", "HumanMessage"]
    assert msgs[-1].content == "thanks"


def test_cut_off_reply_loses_the_turn_today(monkeypatch, chat_dir):
    """Known limitation (BRD gap #44): the turn is written only after the stream completes."""
    monkeypatch.setattr(chat_service, "get_chat_model", lambda: FakeModel(reply='{"search": true, "query": "q"}'))
    monkeypatch.setattr(rag, "search", lambda q: [chunk("t")])

    def broken(msgs, **kw):
        yield FakeMessage("part")
        raise RuntimeError("provider died")

    monkeypatch.setattr(rag, "get_chat_model", lambda: types.SimpleNamespace(stream=broken))
    tokens, _ = chat_service.chat_stream("q", "c3")
    with pytest.raises(RuntimeError):
        "".join(tokens)
    assert memory.get("c3") == []


# ---- memory: last 6 turns, safe file names --------------------------------------------------
def test_memory_returns_only_the_last_six_turns_oldest_first(chat_dir):
    for i in range(10):
        memory.add("c4", "user", f"q{i}")
        memory.add("c4", "assistant", f"a{i}")
    got = memory.get("c4")
    assert len(got) == 12                                              # 6 turns x 2 messages
    assert got[0] == ("user", "q4") and got[-1] == ("assistant", "a9")
    assert len(memory.get_messages("c4")) == 20                        # full history is still stored


def test_conversation_ids_cannot_escape_the_chat_folder(chat_dir):
    memory.add("../../evil", "user", "x")
    names = [p.name for p in chat_dir.iterdir()]
    assert names == ["evil.txt"] and not (chat_dir.parent / "evil.txt").exists()


def test_conversation_title_is_the_first_user_message(chat_dir):
    memory.add("c5", "assistant", "welcome")
    memory.add("c5", "user", "what about diwali leave?")
    assert memory.list_conversations() == [{"id": "c5", "title": "what about diwali leave?"}]
