from functools import lru_cache
import chromadb
from app.config import settings


@lru_cache(maxsize=1)
def get_client():
    return chromadb.PersistentClient(path=settings.chroma_path)


def get_collection():
    # cosine space: distance = 1 - similarity. Chroma embeds text automatically.
    return get_client().get_or_create_collection(
        settings.collection, metadata={"hnsw:space": "cosine"}
    )


def retrieve_scored(query: str, top_k: int | None = None) -> list[dict]:
    """Top-k chunks with similarity scores, NO threshold applied (for debugging)."""
    col = get_collection()
    if col.count() == 0:
        return []
    res = col.query(query_texts=[query], n_results=top_k or settings.top_k)
    out = []
    for doc, meta, dist in zip(
        res["documents"][0], res["metadatas"][0], res["distances"][0]
    ):
        out.append(
            {
                "text": doc,
                "source": meta.get("source"),
                "page": meta.get("page") or None,
                "score": round(1 - dist, 3),
            }
        )
    return out


def retrieve(query: str) -> list[dict]:
    """Top-k chunks that pass the score threshold."""
    return [c for c in retrieve_scored(query) if c["score"] >= settings.score_threshold]