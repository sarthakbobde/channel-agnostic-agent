"""Settings overridden per scope (D31). The Global Settings row stays the global layer; `scoped_settings` holds overrides only.

Precedence is channel, then knowledge base, then department, then global. The registry says which scopes may override each
key: a key cannot be set anywhere else. Like Global Settings, readers see a change within 2 seconds, and if the table cannot
be read the global values are used, so answering never stops because of it.
"""
import logging
import threading
import time
from dataclasses import dataclass

from sqlalchemy import delete, select

from app import global_settings
from app.db import Database, ScopedSetting, get_db

log = logging.getLogger(__name__)
CACHE_SECONDS = 2.0
CHANNEL, KB, DEPARTMENT = "channel", "kb", "department"
SCOPE_TYPES = (CHANNEL, KB, DEPARTMENT)


@dataclass(frozen=True)
class Key:
    name: str
    scopes: tuple


# Validation, labels and defaults come from Global Settings, so one key can never mean two things.
REGISTRY = {k.name: k for k in (
    Key("retrieval_strategy", (KB, DEPARTMENT)),
    Key("hybrid_keyword_weight", (KB, DEPARTMENT)),
    Key("diversity", (KB, DEPARTMENT)),
    Key("top_k", (KB, DEPARTMENT)),
    Key("score_threshold", (KB,)),          # scores from different embedding models are not comparable (D31)
    Key("accepted_languages", (KB, DEPARTMENT)),      # D39
    Key("document_languages", (KB, DEPARTMENT)),
    Key("default_language", (KB, DEPARTMENT)),
    Key("language_confidence", (KB, DEPARTMENT)),
    Key("summary_enabled", (KB,)),                    # D46
    Key("glossary_mode", (KB, DEPARTMENT)),           # D22
    Key("suggestions_enabled", (CHANNEL, KB, DEPARTMENT)),   # D26: on only when the channel and the knowledge base are both on
    Key("feedback_enabled", (CHANNEL, KB, DEPARTMENT)),      # Release 3: same rule as suggestions
    Key("ticket_assignment", (KB, DEPARTMENT)),              # Release 3: who receives new tickets (D58, Phase 2)
    Key("feedback_processing_mode", (KB, DEPARTMENT)),       # Release 3, Phase 3: roll the feedback agents out one knowledge base at a time (D60)
    Key("ticket_alert_channels", (DEPARTMENT,)),
    Key("ticket_alert_events", (DEPARTMENT,)),
)}
RETRIEVAL_KEYS = ("retrieval_strategy", "hybrid_keyword_weight", "diversity", "top_k")

_cache: dict[int, tuple[float, dict]] = {}
_lock = threading.Lock()
_warned_at = 0.0


def invalidate() -> None:
    with _lock:
        _cache.clear()


def _spec(key: str) -> dict:
    return next(f for f in global_settings.SPEC if f["name"] == key)


def _clean(key: str, value, db: Database | None = None, check_catalog: bool = True):
    """Validated value. Stored values are re-read without the catalogue check: a message added in a later release must not
    quietly turn an accepted language list back into "every language"."""
    clean = global_settings.validate({key: value}, global_settings.current(db), check_catalog)[key]
    return list(clean) if isinstance(clean, tuple) else clean


def _overrides(db: Database | None = None) -> dict:
    """{(scope type, scope id, key): value}. A stored value that is no longer valid is ignored."""
    global _warned_at
    try:
        db = db or get_db()
        now = time.monotonic()
        with _lock:
            hit = _cache.get(id(db))
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
        with db.session() as s:
            rows = [(r.scope_type, r.scope_id, r.key, r.value) for r in s.scalars(select(ScopedSetting))]
        out = {}
        for scope_type, scope_id, key, value in rows:
            try:
                if key in REGISTRY:
                    out[(scope_type, scope_id, key)] = _clean(key, value, db, check_catalog=False)
            except ValueError:
                log.warning("ignoring an invalid %s override on %s %s", key, scope_type, scope_id)
        with _lock:
            _cache[id(db)] = (now, out)
        return out
    except Exception:
        if time.monotonic() - _warned_at > 60:
            _warned_at = time.monotonic()
            log.warning("scoped settings unavailable; using the global values", exc_info=True)
        return {}


def _chain(channel=None, kb_id=None, department_id=None) -> list[tuple[str, str]]:
    given = ((CHANNEL, channel), (KB, kb_id), (DEPARTMENT, department_id))
    return [(t, str(i)) for t, i in given if i is not None]


def explain(key: str, *, channel=None, kb_id=None, department_id=None, db: Database | None = None) -> tuple:
    """(value in effect, the scope type it comes from: channel, kb, department or global)."""
    found = _overrides(db)
    for scope_type, scope_id in _chain(channel, kb_id, department_id):
        if scope_type in REGISTRY[key].scopes and (scope_type, scope_id, key) in found:
            return found[(scope_type, scope_id, key)], scope_type
    value = getattr(global_settings.current(db), key)
    return (list(value) if isinstance(value, tuple) else value), "global"


def get(key: str, *, channel=None, kb_id=None, department_id=None, db: Database | None = None):
    return explain(key, channel=channel, kb_id=kb_id, department_id=department_id, db=db)[0]


def for_kb(kb: dict, key: str, db: Database | None = None):
    """A setting of a knowledge base: its own value, then its owning department's, then the global one."""
    return get(key, kb_id=kb.get("id"), department_id=kb.get("department_id"), db=db)


# ---- writing -------------------------------------------------------------------------------------------------------
def _check_scope(scope_type: str, key: str) -> None:
    if key not in REGISTRY:
        raise ValueError(f"Unknown setting: {key}")
    if scope_type not in REGISTRY[key].scopes:
        raise ValueError(f"{_spec(key)['label']} cannot be set for a {scope_type}.")


def put(scope_type: str, scope_id, values: dict, actor: str, db: Database | None = None) -> dict:
    """Set overrides, all or none. Returns {key: (value before or None, value now)}. Raises ValueError for invalid input."""
    db = db or get_db()
    clean = {}
    for key, raw in values.items():
        _check_scope(scope_type, key)
        clean[key] = _clean(key, raw, db)
    changes = {}
    with db.session() as s:
        for key, value in clean.items():
            row = s.scalar(select(ScopedSetting).where(ScopedSetting.scope_type == scope_type, ScopedSetting.scope_id == str(scope_id),
                                                       ScopedSetting.key == key))
            changes[key] = (row.value if row else None, value)
            if row is None:
                s.add(ScopedSetting(scope_type=scope_type, scope_id=str(scope_id), key=key, value=value, updated_by=actor))
            else:
                row.value, row.updated_by = value, actor
    invalidate()
    return changes


def reset(scope_type: str, scope_id, key: str, db: Database | None = None) -> bool:
    """Back to the inherited value. True if there was an override."""
    with (db or get_db()).session() as s:
        n = s.execute(delete(ScopedSetting).where(ScopedSetting.scope_type == scope_type, ScopedSetting.scope_id == str(scope_id),
                                                  ScopedSetting.key == key)).rowcount
    invalidate()
    return bool(n)


def purge(scope_type: str, scope_id, db: Database | None = None) -> None:
    """Its knowledge base or department was deleted: a later one that gets the same id must not inherit these."""
    with (db or get_db()).session() as s:
        s.execute(delete(ScopedSetting).where(ScopedSetting.scope_type == scope_type, ScopedSetting.scope_id == str(scope_id)))
    invalidate()


def view(scope_type: str, scope_id, *, department_id=None, db: Database | None = None) -> list[dict]:
    """The screen for one scope: every key this scope may set, its override, and what it would inherit without one.
    `department_id` is the department a knowledge base belongs to."""
    db = db or get_db()
    found = _overrides(db)
    with db.session() as s:
        meta = {r.key: (r.updated_by, r.updated_at) for r in s.scalars(select(ScopedSetting).where(
            ScopedSetting.scope_type == scope_type, ScopedSetting.scope_id == str(scope_id)))}
    out = []
    for key, reg in REGISTRY.items():
        if scope_type not in reg.scopes:
            continue
        spec = _spec(key)
        inherited, source = explain(key, department_id=department_id if scope_type == KB else None, db=db)
        override = found.get((scope_type, str(scope_id), key))
        out.append({"key": key, "label": spec["label"], "help": spec["help"], "kind": spec.get("kind", "number"),
                    **{f: spec[f] for f in ("min", "max", "step", "choices", "choice_labels", "show_when") if f in spec},
                    "override": override, "overridden": override is not None, "inherited": inherited, "inherited_from": source,
                    "value": override if override is not None else inherited,
                    "updated_by": meta[key][0] if key in meta else "", "updated_at": meta[key][1] if key in meta else None})
    return out
