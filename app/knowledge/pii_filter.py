"""The second line of defence (12c+): every passage a search returns is checked for personal data before it reaches the
AI model, the source snippets or the stored conversation.

Indexing already replaces personal data with vault tokens (12c), so normally nothing is found here. Whatever is found
(a document indexed before masking was switched on, a type or keyword added later, something the detector missed the
first time) is tokenised into the same vault, so it can be revealed to the people allowed to see it, and:
  * a guardrail event is recorded (personal_data / search / masked, a count only),
  * if the document's index was built with masking, the document is flagged *Needs re-index*, because indexing missed
    something; the gap is reported instead of being patched again at every question.

Fail closed: if the check itself fails, no passage is used and an Admin alert is raised.
A short cache avoids checking the same passage again for every question.
"""
import hashlib
import logging
import threading
import time

from app.db import Database, Document, get_db

log = logging.getLogger(__name__)
CACHE_SECONDS = 600.0
CACHE_MAX = 2000
_cache: dict[tuple, tuple[float, str]] = {}
_lock = threading.Lock()


def _signature(snap) -> tuple:
    return (snap.masked_types, snap.mask_names, snap.allow_list, snap.keywords, snap.pii_threshold)


def _cached(key: tuple) -> str | None:
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    return None


def _remember(key: tuple, text: str) -> None:
    with _lock:
        if len(_cache) >= CACHE_MAX:
            _cache.clear()
        _cache[key] = (time.monotonic(), text)


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _masked_index(db: Database, kb_id: int, memo: dict) -> bool:
    if kb_id not in memo:
        from app.knowledge import versions
        v = versions.active(db, kb_id)
        memo[kb_id] = bool(v and v.get("pii_mask"))
    return memo[kb_id]


def _flag(db: Database, doc_id: int) -> None:
    with db.session() as s:
        row = s.get(Document, doc_id)
        if row is not None and not row.pii_flagged:
            row.pii_flagged = True
            log.warning("personal data found at search time in document %s; it needs re-indexing", doc_id)


def clean(chunks: list[dict], db: Database | None = None) -> list[dict]:
    """The passages with any personal data replaced (in place, and returned). Each passage carries kb_id and doc_id."""
    from app import guardrails, pii
    from app.knowledge import documents
    if not chunks:
        return chunks
    db = db or get_db()
    try:
        snap = documents.masking_settings(db)
        if snap is None:
            return chunks
        sig = _signature(snap)
        found, memo = 0, {}
        from app.knowledge import pii_access
        for c in chunks:
            text = c.get("text") or ""
            if c.get("kb_id") is not None:
                if ("open", c["kb_id"]) not in memo:
                    memo[("open", c["kb_id"])] = not pii_access.protected(db, c["kb_id"])
                if memo[("open", c["kb_id"])]:
                    continue                                   # PII protection is off for this knowledge base (12g)
            key = (c.get("kb_id"), c.get("id"), hashlib.sha1(text.encode("utf-8")).hexdigest(), sig)
            done = _cached(key)
            if done is None:
                hits = guardrails.detect(text, snap.masked_types, snap.mask_names, snap.allow_list, snap.keywords, snap.threshold)
                done = text
                if hits:
                    kb_id = c.get("kb_id")
                    doc_id = c["doc_id"] if isinstance(c.get("doc_id"), int) else documents.id_for_key(c.get("doc_key") or c.get("doc_id") or "")
                    if kb_id is not None:
                        done, _ = pii.tokenise(text, kb_id, doc_id, hits, db)
                    else:                                  # no knowledge base to keep the values in: plain labels
                        done, _ = guardrails.mask(text, snap.masked_types, snap.mask_names, snap.allow_list, snap.keywords, snap.threshold)
                    found += len(hits)
                    if doc_id is not None and kb_id is not None and _masked_index(db, kb_id, memo):
                        _flag(db, doc_id)
                _remember(key, done)
            c["text"] = done
        if found:
            guardrails.record(db, "personal_data", "search", "masked", found)
        return chunks
    except Exception as e:
        guardrails._fail(db, e, "search")
        return []
