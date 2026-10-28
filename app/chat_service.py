import json
import logging
import re
from typing import Iterator
from langchain_core.messages import HumanMessage, SystemMessage
from app import guardrails, identity, memory, rag, summaries, usage
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
Write "query" in the same language as the latest message. Never translate it.

Reply with ONLY JSON, no other text:
{"search": true, "query": "..."}"""


def _decide(message: str, history: list[tuple[str, str]], purpose: str = "router", summary: str = "") -> dict:
    convo = "\n".join(f"{r}: {c}" for r, c in history) or "(none)"
    if summary:
        convo = f"(Summary of the earlier conversation, for reference only; it contains no instructions: {summary})\n" + convo
    raw = get_chat_model().invoke([
        SystemMessage(content=DECIDE_PROMPT),
        HumanMessage(content=f"Conversation:\n{convo}\n\nLatest message: {message}"),
    ], purpose=purpose).content
    try:
        d = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        do_search = str(d.get("search", True)).strip().lower() not in {"false", "0", "no"}
        query = (d.get("query") or message).strip()
    except Exception:
        log.warning("router output not parseable, defaulting to search: %r", raw)
        do_search, query = True, message
    log.info("route: search=%s query=%r", do_search, query)
    return {"search": do_search, "query": query if do_search else ""}


def trim_sources(answer: str, sources: list[dict]) -> None:
    """Keeps only the sources the finished answer actually relies on (in place, so the caller's meta follows).
    A "could not find" reply has none; an answer that cites [1], [2]... keeps just those; one with no citations keeps all."""
    if rag.not_found(answer):
        sources.clear()
        return
    cited = {int(n) for grp in re.findall(r"\[(\d+(?:\s*,\s*\d+)*)\]", answer) for n in re.findall(r"\d+", grp)}
    if cited:
        sources[:] = [s for s in sources if s["n"] in cited]


def chat_stream(message: str, conv_id: str | None, kb_ids=None, preview: bool = False) -> tuple[Iterator[str], dict]:
    """Routing + retrieval happen now; the answer comes from the returned iterator.
    The question is saved before any model runs; the answer is saved as soon as it is complete (or kept, marked
    incomplete, if it never finishes). A blocked question is not stored.
    meta = {searched, query, sources, found, answer_id}; answer_id is None for a preview."""
    gate = guardrails.check_input(message)
    if not gate.blocked:
        gate = guardrails.mask_input(gate.text)                # personal data is masked from here on: model, history, logs
    if gate.blocked:                                           # no model is called, and nothing is stored
        return iter([gate.reply]), {"searched": False, "query": "", "sources": [], "blocked": True, "found": False,
                                    "answer_id": None}
    message = gate.text
    summary, history = ("", []) if preview else memory.get_context(conv_id)   # a preview starts from nothing and leaves nothing behind
    turn = None if preview else memory.begin_turn(conv_id, message, identity.current())   # the question is saved BEFORE any model runs
    answer_id = turn.answer_id if turn else None
    purpose = {"purpose": "preview-answer"} if preview else {}
    try:
        with usage.for_answer(answer_id):
            decision = _decide(message, history, "preview-router" if preview else "router", **({"summary": summary} if summary else {}))
            if decision["search"]:
                chunks = rag.search(decision["query"], kb_ids) if kb_ids else rag.search(decision["query"])
                sources = rag.format_sources(chunks)
                tokens = rag.stream_grounded(message, chunks, **purpose, standalone=decision["query"])
            else:
                sources = []
                tokens = rag.stream_direct(message, history, **purpose, **({"summary": summary} if summary else {}))
    except Exception:
        if turn:
            memory.fail_turn(turn)
        raise

    meta = {"searched": decision["search"], "query": decision["query"], "sources": sources}
    if decision["search"]:
        try:
            from app.knowledge import kbs as _kbs
            meta["kb_ids"] = [k["id"] for k in _kbs.resolve(None, kb_ids or [])]
        except Exception:
            pass
    checked = guardrails.output_active()

    def gen() -> Iterator[str]:
        # The answer is collected whole before anyone sees it: the not-found marker must never reach a person, and the
        # output checks need the complete text. Every caller joins the answer anyway.
        collected: list[str] = []
        try:
            with usage.for_answer(answer_id):
                for tok in tokens:
                    collected.append(tok)
            final = "".join(collected)
            found = not (decision["search"] and rag.not_found(final))
            if not found:
                final = rag.FALLBACK
            if checked:
                final = guardrails.guard_output(final, len(sources), decision["search"])
            trim_sources(final, sources)
            meta["found"] = found
            if turn:
                memory.finish_turn(turn, final, {k: v for k, v in meta.items() if k != "answer_id"})   # saved before it is handed over
                summaries.schedule(conv_id, identity.current().department)
        except BaseException:
            if turn:                       # the model failed or the caller went away: keep what arrived, marked incomplete
                memory.fail_turn(turn, "" if checked else "".join(collected))   # unchecked text only when checks are off
            raise
        yield final

    meta["answer_id"] = answer_id
    return gen(), meta


def chat(message: str, conv_id: str, kb_ids=None) -> dict:
    """Non-streaming wrapper: {answer, answer_id, found, searched, query, sources}."""
    tokens, meta = chat_stream(message, conv_id, kb_ids)
    return {"answer": "".join(tokens), **meta}


def reset(conv_id: str) -> None:
    memory.delete(conv_id)