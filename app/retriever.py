import logging
from functools import lru_cache
import chromadb
from app import global_settings
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


def _search_kb(kb: dict, query: str, k: int, who) -> list[dict]:
    """One knowledge base, with the asker's access applied INSIDE the vector search."""
    from app.db import get_db
    from app.knowledge import access, kbs
    db = get_db()
    if not access.can_query(db, kb, who):
        return []
    keys = access.visible_doc_keys(db, kb, who)
    if not keys:
        return []
    hits = kbs.store_for(kb).search(query, k, {"doc_id": {"in": keys}})
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
             "page": h["metadata"].get("page") or None, "score": h["score"]} for h in merged]


def retrieve(query: str, kb_ids=None) -> list[dict]:
    """Top-k passages that pass the score threshold."""
    scored = retrieve_scored(query, kb_ids=kb_ids) if kb_ids else retrieve_scored(query)
    return [c for c in scored if c["score"] >= global_settings.current().score_threshold]
