import logging
from functools import lru_cache
import chromadb
from app import global_settings, keyword_search
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


def _search_kb(kb: dict, query: str, k: int, who) -> list[dict]:
    """One knowledge base, with the asker's access applied INSIDE the search, for every strategy."""
    from app.db import get_db
    from app.knowledge import access, kbs
    db = get_db()
    if not access.can_query(db, kb, who):
        return []
    keys = access.visible_doc_keys(db, kb, who)
    if not keys:
        return []
    snap = global_settings.current()
    strategy = snap.retrieval_strategy
    flt = {"doc_id": {"in": keys}}
    try:
        if strategy == "keyword":
            hits = [{**h, "via": "keywords"} for h in _keyword_hits(kb, query, k, keys)]
        elif strategy == "hybrid":
            n = max(k * 3, 20)
            vector = kbs.store_for(kb).search(query, n, flt)
            hits = _fuse(vector, _keyword_hits(kb, query, n, keys), snap.hybrid_keyword_weight)[:k]
        elif strategy == "mmr":
            hits = _diverse([{**h, "via": "meaning"} for h in kbs.store_for(kb).search(query, max(k * 4, 20), flt)], k, snap.diversity)
        else:
            hits = [{**h, "via": "meaning"} for h in kbs.store_for(kb).search(query, k, flt)]
    except NotImplementedError:
        log.warning("%s search is not available for this store; using similarity", strategy)
        hits = [{**h, "via": "meaning"} for h in kbs.store_for(kb).search(query, k, flt)]
    safe = [h for h in hits if access.permits(h["metadata"], who, kb)]
    if len(safe) != len(hits):
        log.error("access filter let through %d passage(s) the chunk metadata does not permit; dropped", len(hits) - len(safe))
    return safe


def retrieve_scored(query: str, top_k: int | None = None, kb_ids=None) -> list[dict]:
    """Top-k passages the CURRENT asker may see, with similarity scores, NO threshold applied.
    Several knowledge bases are searched separately and merged by reciprocal rank fusion (D5)."""
    from app import identity
    who = identity.current()
    k = top_k or global_settings.current().top_k
    lists = [_search_kb(kb, query, k, who) for kb in _targets(kb_ids)]
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
    return [{"text": h["text"], "source": h["metadata"].get("source"),
             "page": h["metadata"].get("page") or None, "score": h["score"], "via": h.get("via", "meaning")} for h in merged]


def retrieve(query: str, kb_ids=None) -> list[dict]:
    """Top-k passages that pass the score threshold."""
    scored = retrieve_scored(query, kb_ids=kb_ids) if kb_ids else retrieve_scored(query)
    return [c for c in scored if c["score"] >= global_settings.current().score_threshold]
