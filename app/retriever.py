import logging
from functools import lru_cache
import chromadb
from app import global_settings, glossary, keyword_search, scoped_settings, trace
from app.config import settings

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_client():
    return chromadb.PersistentClient(path=settings.chroma_path)


def get_collection():
    # cosine space: distance = 1 - similarity. Chroma embeds text automatically.
    return get_client().get_or_create_collection(
        settings.collection, metadata={"hnsw:space": "cosine"}
    )


RRF_K = 60


def _targets(kb_ids) -> list[dict]:
    """The knowledge bases to search. None named -> the default one."""
    from app.knowledge import kbs
    found = kbs.resolve(None, kb_ids or [])
    return found


def _keyword_hits(kb: dict, query: str, k: int, keys) -> list[dict]:
    from app.db import get_db
    from app.knowledge import kbs, versions
    store = kbs.store_for(kb)
    active = versions.active(get_db(), kb["id"]) if kb.get("id") else None
    return keyword_search.index_for(kb.get("id"), active and active["version"], store).search(query, k, keys)


def _fuse(vector: list[dict], keyword: list[dict], keyword_weight: float) -> list[dict]:
    """Weighted reciprocal rank fusion of the two lists. A passage found by both is marked so; its score is the better of the two."""
    wv, wk = 1.0 - keyword_weight, keyword_weight
    merged: dict[str, dict] = {}
    for rank, h in enumerate(vector, 1):
        merged[h["id"]] = {**h, "via": "meaning", "_rrf": wv / (RRF_K + rank)}
    for rank, h in enumerate(keyword, 1):
        if h["id"] in merged:
            m = merged[h["id"]]
            m["_rrf"] += wk / (RRF_K + rank)
            m["via"] = "both"
            m["score"] = max(m["score"], h["score"])
        else:
            merged[h["id"]] = {**h, "via": "keywords", "_rrf": wk / (RRF_K + rank)}
    return sorted((m for m in merged.values() if m["_rrf"] > 0), key=lambda m: -m["_rrf"])


def _diverse(hits: list[dict], k: int, diversity: float) -> list[dict]:
    """Maximal marginal relevance: keep relevant passages but skip ones that repeat what is already chosen."""
    lam = 1.0 - diversity
    chosen: list[dict] = []
    pool = list(hits)
    while pool and len(chosen) < k:
        best = max(pool, key=lambda h: lam * h["score"] - (1 - lam) * max((keyword_search.overlap(h["text"], c["text"]) for c in chosen), default=0.0))
        chosen.append(best)
        pool.remove(best)
    return chosen


def threshold_for(kb: dict) -> float:
    """The lowest similarity a passage from this knowledge base may have. Scores from different embedding models are not
    comparable, so it is applied per knowledge base, before several are merged. A knowledge base may set its own (D31)."""
    return scoped_settings.get("score_threshold", kb_id=kb.get("id"))


def _active_version(kb: dict):
    from app.db import get_db
    from app.knowledge import versions
    v = versions.active(get_db(), kb["id"]) if kb.get("id") else None
    return v and v["version"]


def document_languages(kb: dict) -> list[str]:
    """The languages a knowledge base's documents are written in: its setting, else what was detected in its documents."""
    chosen = scoped_settings.for_kb(kb, "document_languages")
    if chosen or not kb.get("id"):
        return list(chosen or [])
    from sqlalchemy import distinct, select
    from app.db import Document, get_db
    with get_db().session() as s:
        return sorted(l for l in s.scalars(select(distinct(Document.language)).where(Document.kb_id == kb["id"], Document.language != "")))


def _search_kb(kb: dict, query: str, k: int, who, min_score: float | None = None) -> list[dict]:
    """One knowledge base, with the asker's access applied INSIDE the search, for every strategy.
    Each hit carries the Document id of its passage (`doc_pk`)."""
    from app import language
    from app.db import get_db
    from app.knowledge import access, kbs
    db = get_db()
    if not access.can_query(db, kb, who):
        return []
    docs = access.visible_docs(db, kb, who)
    keys = list(docs)
    if not keys:
        return []
    conf = {key: scoped_settings.for_kb(kb, key) for key in scoped_settings.RETRIEVAL_KEYS}
    strategy = conf["retrieval_strategy"]
    turn = language.current()
    lang = turn.resolved if turn is not None else ""
    vector_q = keyword_q = query
    gmode = glossary.mode(kb)
    if gmode != "off":
        # D22: only the search text changes. Index mode: stable terms are already in the passages, so the meaning search adds
        # only the others; keywords are matched literally, so the keyword search gets every term.
        every = glossary.expand(query, kb, lang)
        keyword_q = every.search_text
        vector_q = keyword_q if gmode == "query" else glossary.expand(query, kb, lang, stable=False).search_text
        trace.note_glossary(kb, gmode, every, vector_q)
    if turn is not None:
        langs = document_languages(kb)
        turn.doc_languages[str(kb.get("id"))] = langs
        conf["doc_languages"] = langs
        if strategy in ("keyword", "hybrid") and langs and turn.resolved not in langs:
            # D35: keywords cannot match across languages, so a cross-language question is searched by meaning only
            conf["keywords_skipped"] = True
            strategy = "similarity"
    flt = {"doc_id": {"in": keys}}
    try:
        if strategy == "keyword":
            hits = [{**h, "via": "keywords"} for h in _keyword_hits(kb, keyword_q, k, keys)]
        elif strategy == "hybrid":
            n = max(k * 3, 20)
            vector = kbs.store_for(kb).search(vector_q, n, flt)
            hits = _fuse(vector, _keyword_hits(kb, keyword_q, n, keys), conf["hybrid_keyword_weight"])[:k]
        elif strategy == "mmr":
            hits = _diverse([{**h, "via": "meaning"} for h in kbs.store_for(kb).search(vector_q, max(k * 4, 20), flt)], k, conf["diversity"])
        else:
            hits = [{**h, "via": "meaning"} for h in kbs.store_for(kb).search(vector_q, k, flt)]
    except NotImplementedError:
        log.warning("%s search is not available for this store; using similarity", strategy)
        hits = [{**h, "via": "meaning"} for h in kbs.store_for(kb).search(vector_q, k, flt)]
    safe = [h for h in hits if access.permits(h["metadata"], who, kb)]
    if len(safe) != len(hits):
        log.error("access filter let through %d passage(s) the chunk metadata does not permit; dropped", len(hits) - len(safe))
    safe = [{**h, "kb_id": kb.get("id"), "doc_pk": docs.get(h["metadata"].get("doc_id"))} for h in safe]
    trace.note_search(kb, {**conf, "top_k": k}, min_score, safe, lambda: _active_version(kb))
    return [h for h in safe if min_score is None or h["score"] >= min_score]


def retrieve_scored(query: str, top_k: int | None = None, kb_ids=None, apply_threshold: bool = False) -> list[dict]:
    """Top-k passages the CURRENT asker may see, with similarity scores. With `apply_threshold`, each knowledge base's
    threshold is applied to its own passages first; otherwise none is applied.
    Several knowledge bases are searched separately and merged by reciprocal rank fusion (D5)."""
    from app import identity
    who = identity.current()
    targets = [(kb, top_k or scoped_settings.for_kb(kb, "top_k")) for kb in _targets(kb_ids)]
    k = max((n for _, n in targets), default=0)                  # several knowledge bases: the merged list keeps the largest
    lists = [_search_kb(kb, query, n, who, threshold_for(kb) if apply_threshold else None) for kb, n in targets]
    lists = [l for l in lists if l]
    if len(lists) > 1:
        fused: dict[tuple, list] = {}
        for hits in lists:
            for rank, h in enumerate(hits, 1):
                entry = fused.setdefault((id(hits), rank), [0.0, h])
                entry[0] += 1.0 / (RRF_K + rank)
        merged = [h for _, h in sorted(fused.values(), key=lambda e: -e[0])][:k]
    else:
        merged = lists[0] if lists else []
    trace.note_used([{"kb_id": h.get("kb_id"), "chunk_id": h.get("id")} for h in merged])
    return [{"text": h["text"], "source": h["metadata"].get("source"),
             "page": h["metadata"].get("page") or None, "score": h["score"], "via": h.get("via", "meaning"),
             "doc_id": h.get("doc_pk"), "kb_id": h.get("kb_id"), "chunk_id": h.get("id"), "id": h.get("id"),
             "doc_key": h["metadata"].get("doc_id"), "title": h["metadata"].get("source"),
             "doc_language": h["metadata"].get("doc_lang") or h["metadata"].get("lang") or ""} for h in merged]


def retrieve(query: str, kb_ids=None) -> list[dict]:
    """Top-k passages that pass their knowledge base's score threshold (D31), checked for personal data before anything
    else sees them."""
    from app.knowledge import pii_access, pii_filter
    scored = retrieve_scored(query, kb_ids=kb_ids, apply_threshold=True) if kb_ids else retrieve_scored(query, apply_threshold=True)
    return pii_access.open_passages(pii_filter.clean(scored))
