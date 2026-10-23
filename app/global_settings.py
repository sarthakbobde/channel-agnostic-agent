"""Runtime-editable settings that used to be .env lines (top-k, score threshold, history, chunking).

One row in `global_settings`, created on first read from the .env values (so an upgrade keeps
behaving exactly as before). Readers call `current()`; a 2-second cache means an admin's change
reaches every reader within seconds, with no restart. If the database cannot be read, the .env
values are used and a warning is logged, so answering never stops because of this table.
"""
import logging
import threading
import time
from dataclasses import asdict, dataclass

from sqlalchemy.exc import IntegrityError

from app.config import settings as env
from app.db import Database, GlobalSettings, get_db

log = logging.getLogger(__name__)
CACHE_SECONDS = 2.0


@dataclass(frozen=True)
class Snapshot:
    top_k: int
    score_threshold: float
    recent_turns: int
    chunk_size: int
    chunk_overlap: int
    max_retention_days: int
    prompt_max_chars: int
    summarise_after_turns: int
    retention_job_enabled: bool
    export_enabled: bool
    chat_rate_per_minute: int
    retrieval_strategy: str
    hybrid_keyword_weight: float
    diversity: float


STRATEGIES = {
    "similarity": "Similarity (meaning)",
    "keyword": "Keyword (exact words)",
    "hybrid": "Hybrid (meaning + keywords)",
    "mmr": "Similarity with variety",
}


# What the admin screen shows and what is accepted. `integer` fields reject fractions.
SPEC = (
    {"name": "retrieval_strategy", "label": "How passages are found", "kind": "choice", "choices": list(STRATEGIES), "choice_labels": STRATEGIES,
     "help": "Similarity finds passages with the same meaning (best for ordinary questions). Keyword finds the exact words (best for codes, names and numbers). Hybrid does both and merges the results. Similarity with variety avoids sending near-identical passages. Default similarity."},
    {"name": "hybrid_keyword_weight", "show_when": {"field": "retrieval_strategy", "values": ["hybrid"]}, "label": "Hybrid: weight of keywords", "min": 0.0, "max": 1.0, "step": 0.05, "integer": False,
     "help": "Only used by Hybrid. From 0 to 1: 0 follows meaning only, 1 follows keywords only, 0.5 weighs them equally. Default 0.5."},
    {"name": "diversity", "show_when": {"field": "retrieval_strategy", "values": ["mmr"]}, "label": "Variety between passages", "min": 0.0, "max": 1.0, "step": 0.05, "integer": False,
     "help": "Only used by Similarity with variety. From 0 to 1: higher skips passages that repeat each other. Default 0.3."},
    {"name": "top_k", "label": "Passages used per question", "min": 1, "max": 50, "step": 1, "integer": True,
     "help": "How many of the best-matching passages from your documents are given to the AI model with each question. More gives it more to work with but costs more. Default 5."},
    {"name": "score_threshold", "label": "How closely a passage must match", "min": 0.0, "max": 1.0, "step": 0.01, "integer": False,
     "help": "From 0 to 1. Passages that match less well than this are ignored. If nothing matches well enough, the assistant says it could not find the answer. Default 0.35."},
    {"name": "recent_turns", "label": "Recent question-and-answer pairs the AI model remembers", "min": 1, "max": 50, "step": 1, "integer": True,
     "help": "How many of the latest question-and-answer pairs the AI model sees with each new question. Default 6."},
    {"name": "chunk_size", "label": "Passage size (characters)", "min": 100, "max": 8000, "step": 50, "integer": True,
     "help": "How long each piece of a document is when it is split for searching. Applies the next time documents are indexed. Default 900."},
    {"name": "chunk_overlap", "label": "Overlap between passages (characters)", "min": 0, "max": 7999, "step": 10, "integer": True,
     "help": "How much neighbouring passages share, so a sentence at the edge is not lost. Must be smaller than the passage size. Default 150."},
    {"name": "max_retention_days", "label": "Longest time conversations may be kept (days)", "min": 1, "max": 3650, "step": 1, "integer": True,
     "help": "No department can keep conversations longer than this. Each department chooses its own period, up to this. Default 365."},
    {"name": "prompt_max_chars", "label": "Longest department prompt (characters)", "min": 200, "max": 20000, "step": 100, "integer": True,
     "help": "A department prompt longer than this is rejected when it is saved. Default 4000."},
    {"name": "summarise_after_turns", "label": "Summarise a long chat after (question-answer pairs)", "min": 4, "max": 100, "step": 1, "integer": True,
     "help": "When this many pairs have not been summarised yet, the older ones are summarised in the background so the AI model keeps the gist. The most recent messages (see above) are always kept word for word. Default 12."},
    {"name": "chat_rate_per_minute", "label": "Questions per person per minute", "min": 0, "max": 600, "step": 1, "integer": True,
     "help": "Slows down anyone who asks more than this many questions in a minute, in the Web UI, the API and the chat apps. 0 means no limit. Default 20."},
    {"name": "retention_job_enabled", "label": "Delete expired conversations every day", "kind": "bool",
     "help": "A daily job deletes conversations that are older than their department's keep-for period. Every run is recorded in the audit log."},
    {"name": "export_enabled", "label": "Allow exporting conversations", "kind": "bool",
     "help": "Off by default. Turn it on only after the privacy sign-off. An export needs a written reason, only Admins can do it, and every export is recorded in the audit log."},
)
_BY_NAME = {f["name"]: f for f in SPEC}

_cache: dict[int, tuple[float, Snapshot]] = {}
_lock = threading.Lock()
_warned_at = 0.0


def defaults() -> Snapshot:
    """The .env values (also the first-run seed)."""
    return Snapshot(top_k=env.top_k, score_threshold=env.score_threshold, recent_turns=env.memory_max_turns,
                    chunk_size=env.chunk_size, chunk_overlap=env.chunk_overlap,
                    max_retention_days=env.max_retention_days, prompt_max_chars=env.prompt_max_chars,
                    summarise_after_turns=env.summarise_after_turns, retention_job_enabled=True, export_enabled=False,
                    chat_rate_per_minute=20, retrieval_strategy="similarity", hybrid_keyword_weight=0.5, diversity=0.3)


def _snapshot(row: GlobalSettings) -> Snapshot:
    return Snapshot(row.top_k, row.score_threshold, row.recent_turns, row.chunk_size, row.chunk_overlap,
                    row.max_retention_days, row.prompt_max_chars, row.summarise_after_turns, row.retention_job_enabled,
                    row.export_enabled, row.chat_rate_per_minute, row.retrieval_strategy, row.hybrid_keyword_weight, row.diversity)


def _read(db: Database) -> tuple[Snapshot, GlobalSettings]:
    for _ in range(2):
        with db.session() as s:
            row = s.get(GlobalSettings, 1)
            if row is not None:
                return _snapshot(row), row
            try:
                s.add(GlobalSettings(id=1, updated_by="system (seeded from .env)", **asdict(defaults())))
                s.flush()
            except IntegrityError:           # another process seeded it first
                s.rollback()
    raise RuntimeError("could not read or seed global settings")


def invalidate() -> None:
    with _lock:
        _cache.clear()


def current(db: Database | None = None) -> Snapshot:
    global _warned_at
    try:
        db = db or get_db()
        now = time.monotonic()
        with _lock:
            hit = _cache.get(id(db))
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
        snap, _ = _read(db)
        with _lock:
            _cache[id(db)] = (now, snap)
        return snap
    except Exception:
        if time.monotonic() - _warned_at > 60:           # do not flood the log on every question
            _warned_at = time.monotonic()
            log.warning("global settings unavailable; using .env defaults", exc_info=True)
        return defaults()


def validate(values: dict, base: Snapshot) -> dict:
    """Clean and check submitted values; returns only the fields to change. Raises ValueError
    with a message an admin can act on."""
    unknown = sorted(set(values) - set(_BY_NAME))
    if unknown:
        raise ValueError("Unknown setting: " + ", ".join(unknown))
    clean = {}
    for name, raw in values.items():
        f = _BY_NAME[name]
        if f.get("kind") == "choice":
            if raw not in f["choices"]:
                raise ValueError(f"{f['label']} must be one of: " + ", ".join(f["choices"]) + ".")
            clean[name] = raw
            continue
        if f.get("kind") == "bool":
            if not isinstance(raw, bool):
                raise ValueError(f"{f['label']} must be on or off.")
            clean[name] = raw
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"{f['label']} must be a number.")
        if f["integer"]:
            if raw != int(raw):
                raise ValueError(f"{f['label']} must be a whole number.")
            raw = int(raw)
        else:
            raw = float(raw)
        if not f["min"] <= raw <= f["max"]:
            raise ValueError(f"{f['label']} must be between {f['min']} and {f['max']}.")
        clean[name] = raw
    merged = {**asdict(base), **clean}
    if merged["chunk_overlap"] >= merged["chunk_size"]:
        raise ValueError("Overlap must be smaller than the passage size.")
    return clean


def view(db: Database | None = None) -> dict:
    """The settings screen: field definitions with current values."""
    db = db or get_db()
    snap, row = _read(db)
    values = asdict(snap)
    fields = [{"name": f["name"], "label": f["label"], "kind": "choice", "help": f["help"], "value": values[f["name"]],
               "choices": f["choices"], "choice_labels": f["choice_labels"]}
              if f.get("kind") == "choice" else
              {"name": f["name"], "label": f["label"], "kind": "bool", "help": f["help"], "value": values[f["name"]]}
              if f.get("kind") == "bool" else
              {"name": f["name"], "label": f["label"], "kind": "number", "help": f["help"],
               "min": f["min"], "max": f["max"], "step": f["step"], "value": values[f["name"]], **({"show_when": f["show_when"]} if f.get("show_when") else {})}
              for f in SPEC]
    return {"fields": fields, "updated_at": row.updated_at, "updated_by": row.updated_by}


def update(values: dict, actor: str, db: Database | None = None) -> tuple[Snapshot, Snapshot]:
    """Apply changes. Returns (before, after). Raises ValueError for invalid input."""
    db = db or get_db()
    before, _ = _read(db)
    clean = validate(values, before)
    if clean:
        with db.session() as s:
            row = s.get(GlobalSettings, 1)
            for k, v in clean.items():
                setattr(row, k, v)
            row.updated_by = actor
        invalidate()
    after, _ = _read(db)
    return before, after
