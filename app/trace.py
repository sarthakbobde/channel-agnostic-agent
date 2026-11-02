"""The answer trace: how one answer came about, so it can be explained later (D23).

A Recorder collects references through the turn: passage ids and scores, prompt versions, the settings and models in effect,
and timings. It never holds passage text or the rendered prompt. Deep code (retriever, prompts, model chain) reports through
the `note_*` functions, which do nothing outside a recorded turn. Nothing here may ever break an answer: every failure is
swallowed and logged by type only, because a database error's text can quote the question.
"""
import contextlib
import contextvars
import functools
import hashlib
import json
import logging
import time

from app.db import AnswerTrace, Database, get_db

log = logging.getLogger(__name__)
_recorder: contextvars.ContextVar["Recorder | None"] = contextvars.ContextVar("trace_recorder", default=None)


def _safe(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kw):
        try:
            return fn(*args, **kw)
        except Exception as e:
            log.warning("trace: %s failed (%s)", fn.__name__, type(e).__name__)
    return wrapper


class Recorder:
    def __init__(self):
        self.standalone_query = self.search_text = ""
        self.hits: list[dict] = []
        self.retrieval: list[dict] = []
        self.prompts: dict[str, dict] = {}
        self.models: list[dict] = []
        self.timings: dict[str, int] = {}
        self.language = None               # the turn's LanguageContext (Phase 2)
        self.glossary: dict = {}           # kb id -> {mode, versions, applied, search_text} (Phase 3)
        self.saved = False

    @contextlib.contextmanager
    def stage(self, name: str):
        """Time one stage of the turn, in milliseconds; a stage that runs twice adds up."""
        start = time.perf_counter()
        try:
            yield
        finally:
            self.timings[name] = self.timings.get(name, 0) + round((time.perf_counter() - start) * 1000)

    def answering_model(self) -> dict:
        """The model that wrote the answer: the last successful call that was not the router's."""
        return next((m for m in reversed(self.models) if "router" not in m["purpose"]), {})

    def snapshot_id(self) -> str:
        """A hash of what was in effect: two answers with the same id had the same models, prompts, indexes and settings."""
        config = {"models": sorted({(m["provider"], m["model"]) for m in self.models}),
                  "prompts": {k: v["id"] for k, v in self.prompts.items()}, "retrieval": self.retrieval}
        if self.language is not None:
            config["language"] = {"resolved": self.language.resolved, "accepted": self.language.accepted}
        if self.glossary:
            config["glossary"] = {k: {"mode": v["mode"], "versions": v["versions"]} for k, v in self.glossary.items()}
        return hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()[:16]


@contextlib.contextmanager
def recording(rec: Recorder):
    token = _recorder.set(rec)
    try:
        yield
    finally:
        _recorder.reset(token)


@_safe
def note_search(kb: dict, conf: dict, threshold: float | None, hits: list[dict], index_version) -> None:
    """One knowledge base was searched. `hits` are the passages the asker may see, before the threshold."""
    rec = _recorder.get()
    if rec is None:
        return
    rec.retrieval.append({"kb_id": kb.get("id"), "index_version": index_version(), "threshold": threshold, **conf})
    rec.hits += [{"kb_id": kb.get("id"), "doc_id": h.get("doc_pk"), "chunk_id": h.get("id"), "score": h.get("score"), "rank": rank,
                  "via": h.get("via", "meaning"), "above_threshold": threshold is None or h.get("score", 0) >= threshold, "used": False}
                 for rank, h in enumerate(hits, 1)]


@_safe
def note_glossary(kb: dict, mode: str, expansion, vector_text: str) -> None:
    """The glossary terms applied to one knowledge base's search, and the text each search used."""
    rec = _recorder.get()
    if rec is None:
        return
    rec.glossary[str(kb.get("id"))] = {"mode": mode, "versions": expansion.versions, "applied": expansion.applied,
                                       "search_text": expansion.search_text, "vector_text": vector_text}
    if expansion.applied and rec.search_text == rec.standalone_query:
        rec.search_text = vector_text


@_safe
def note_used(chunks: list[dict]) -> None:
    """These passages survived the merge and went to the model."""
    rec = _recorder.get()
    if rec is None:
        return
    used = {(c.get("kb_id"), c.get("chunk_id")) for c in chunks}
    for h in rec.hits:
        h["used"] = (h["kb_id"], h["chunk_id"]) in used


@_safe
def note_prompt(scope: str, version: dict) -> None:
    rec = _recorder.get()
    if rec is not None:
        rec.prompts[scope] = {"id": version["id"], "version": version["version"]}


@_safe
def note_model(provider: str, model: str, purpose: str, fallback: bool) -> None:
    rec = _recorder.get()
    if rec is not None:
        rec.models.append({"provider": provider, "model": model, "purpose": purpose, "fallback": fallback})


@_safe
def save(rec: Recorder, turn, *, complete: bool, found: bool, searched: bool, db: Database | None = None) -> None:
    """Write the trace of one answer, once. Its own transaction: a failure here leaves the saved answer untouched."""
    if rec.saved:
        return
    used = rec.answering_model()
    lang = rec.language
    with (db or get_db()).session() as s:
        s.add(AnswerTrace(answer_id=turn.answer_id, question_id=turn.question_id, conversation_id=turn.conversation_id, complete=complete,
                          found=found, searched=searched, standalone_query=rec.standalone_query, search_text=rec.search_text,
                          hits=rec.hits, retrieval=rec.retrieval, prompt_versions=rec.prompts, provider=used.get("provider", ""),
                          model=used.get("model", ""), fallback_used=any(m["fallback"] for m in rec.models), timings=rec.timings,
                          glossary=rec.glossary, config_snapshot_id=rec.snapshot_id(), language=lang.resolved if lang else "",
                          language_confidence=lang.confidence if lang else None, language_detail=lang.as_dict() if lang else None))
    rec.saved = True
