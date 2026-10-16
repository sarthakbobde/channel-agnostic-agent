import json
import logging
import re
from typing import Iterator
from langchain_core.messages import HumanMessage, SystemMessage
from app import memory, rag
from app.llm import get_chat_model

log = logging.getLogger(__name__)

DECIDE_PROMPT = """You are the router of a company knowledge-base assistant.
Decide whether the user's latest message needs a search of the knowledge base.

search=true  : any question about facts, policies, procedures, people, tools, or anything
               that could be written in company documents. When unsure, choose true.
search=false : ONLY greetings, thanks, small talk, or questions about the conversation itself
               (e.g. "summarise what you just said").

If search is true, "query" must be a standalone search query. Use the conversation to resolve
follow-ups and pronouns (e.g. "what about contractors?" -> "annual leave entitlement for contractors").

Reply with ONLY JSON, no other text:
{"search": true, "query": "..."}"""


def _decide(message: str, history: list[tuple[str, str]]) -> dict:
    convo = "\n".join(f"{r}: {c}" for r, c in history) or "(none)"
    raw = get_chat_model().invoke([
        SystemMessage(content=DECIDE_PROMPT),
        HumanMessage(content=f"Conversation:\n{convo}\n\nLatest message: {message}"),
    ], purpose="router").content
    try:
        d = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        do_search = str(d.get("search", True)).strip().lower() not in {"false", "0", "no"}
        query = (d.get("query") or message).strip()
    except Exception:
        log.warning("router output not parseable, defaulting to search: %r", raw)
        do_search, query = True, message
    log.info("route: search=%s query=%r", do_search, query)
    return {"search": do_search, "query": query if do_search else ""}


def chat_stream(message: str, conv_id: str, kb_ids=None) -> tuple[Iterator[str], dict]:
    """Routing + retrieval happen now; the answer streams from the returned iterator.
    The turn is saved to the DB once the iterator is fully consumed.
    meta = {searched, query, sources}"""
    history = memory.get(conv_id)
    decision = _decide(message, history)

    if decision["search"]:
        chunks = rag.search(decision["query"], kb_ids) if kb_ids else rag.search(decision["query"])
        sources = rag.format_sources(chunks)
        tokens = rag.stream_grounded(decision["query"], chunks)
    else:
        sources = []
        tokens = rag.stream_direct(message, history)

    meta = {"searched": decision["search"], "query": decision["query"], "sources": sources}

    def gen() -> Iterator[str]:
        parts: list[str] = []
        for tok in tokens:
            parts.append(tok)
            yield tok
        memory.add(conv_id, "user", message)
        memory.add(conv_id, "assistant", "".join(parts), meta)

    return gen(), meta


def chat(message: str, conv_id: str, kb_ids=None) -> dict:
    """Non-streaming wrapper: {answer, searched, query, sources}."""
    tokens, meta = chat_stream(message, conv_id, kb_ids)
    return {"answer": "".join(tokens), **meta}


def reset(conv_id: str) -> None:
    memory.delete(conv_id)