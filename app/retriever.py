from functools import lru_cache
import chromadb
from app import global_settings
from app.config import settings


@lru_cache(maxsize=1)
def get_client():
    return chromadb.PersistentClient(path=settings.chroma_path)


def get_collection():
    # cosine space: distance = 1 - similarity. Chroma embeds text automatically.
    return get_client().get_or_create_collection(
        settings.collection, metadata={"hnsw:space": "cosine"}
    )


def _store(kb_ids):
    """The vector store to search. No knowledge base named -> the original collection (no database needed).
    Several named -> only the first for now (merging across knowledge bases arrives later)."""
    from app.knowledge import kbs
    if not kb_ids:
        return kbs.default_store()
    found = kbs.resolve(None, [kb_ids[0]])
    return kbs.store_for(found[0]) if found else kbs.default_store()


def retrieve_scored(query: str, top_k: int | None = None, kb_ids=None) -> list[dict]:
    """Top-k chunks with similarity scores, NO threshold applied (for debugging)."""
    hits = _store(kb_ids).search(query, top_k or global_settings.current().top_k)
    return [{"text": h["text"], "source": h["metadata"].get("source"),
             "page": h["metadata"].get("page") or None, "score": h["score"]} for h in hits]


def retrieve(query: str, kb_ids=None) -> list[dict]:
    """Top-k chunks that pass the score threshold."""
    scored = retrieve_scored(query, kb_ids=kb_ids) if kb_ids else retrieve_scored(query)
    return [c for c in scored if c["score"] >= global_settings.current().score_threshold]
