"""Vector stores behind one interface.

Operations: add, search(filter), delete_by_document, delete, update_metadata, count, health, stats.
One filter format for every store: {"field": value} (equals) or {"field": {"in": [...]}}; several keys
are ANDed. Each adapter translates it (Chroma `where` today; SQL WHERE / OData for later stores).
"""
from abc import ABC, abstractmethod
from typing import Callable


def to_chroma_where(flt: dict | None) -> dict | None:
    if not flt:
        return None
    parts = []
    for key, cond in flt.items():
        if isinstance(cond, dict):
            if set(cond) != {"in"}:
                raise ValueError(f"unsupported filter for {key!r}")
            parts.append({key: {"$in": list(cond["in"])}})
        else:
            parts.append({key: {"$eq": cond}})
    return parts[0] if len(parts) == 1 else {"$and": parts}


class VectorStore(ABC):
    name: str

    @abstractmethod
    def add(self, ids: list[str], texts: list[str], metadatas: list[dict]) -> None: ...

    @abstractmethod
    def search(self, query: str, k: int, flt: dict | None = None) -> list[dict]:
        """-> [{id, text, metadata, score}], best first; score = similarity (1 = identical)."""

    @abstractmethod
    def delete_by_document(self, doc_id: str) -> None: ...

    @abstractmethod
    def delete(self) -> None:
        """Remove the whole index."""

    @abstractmethod
    def update_metadata(self, patch: dict, *, document: str | None = None, ids: list[str] | None = None) -> int:
        """Merge `patch` into the metadata of a document's chunks (or the given ids) without re-embedding.
        Returns how many chunks were changed."""

    @abstractmethod
    def replace_metadata(self, ids: list[str], metadatas: list[dict]) -> None:
        """Set each chunk's metadata to the given values (per id) without re-embedding."""

    @abstractmethod
    def count(self) -> int: ...

    @abstractmethod
    def health(self) -> tuple[bool, str]: ...

    @abstractmethod
    def stats(self) -> dict:
        """{chunks, documents}"""

    @abstractmethod
    def iter_metadata(self, batch: int = 500):
        """Yield (ids, metadatas) pages: used for one-off metadata back-fills."""

    def iter_texts(self, batch: int = 500):
        """Yield (ids, texts, metadatas) pages: the whole index, for keyword search. A store that cannot list its text
        raises NotImplementedError and keyword search falls back to similarity."""
        raise NotImplementedError(f"{self.name} cannot list its passages")


class ChromaStore(VectorStore):
    name = "chroma"

    def __init__(self, get_collection: Callable, delete_collection: Callable | None = None, embeddings=None):
        self._col, self._drop = get_collection, delete_collection
        self.embeddings = embeddings if embeddings is not None and not embeddings.builtin else None

    def add(self, ids, texts, metadatas):
        if not ids:
            return
        kw = {"ids": ids, "documents": texts, "metadatas": metadatas}
        if self.embeddings is not None:
            kw["embeddings"] = self.embeddings.embed_documents(texts)
        self._col().upsert(**kw)

    def search(self, query, k, flt=None):
        if any(isinstance(c, dict) and not c.get("in", [None]) for c in (flt or {}).values()):
            return []                                  # an empty allow-list matches nothing (Chroma itself refuses an empty list)
        col = self._col()
        if col.count() == 0:
            return []
        kw = {"n_results": k}
        where = to_chroma_where(flt)
        if where:
            kw["where"] = where
        if self.embeddings is not None:
            kw["query_embeddings"] = [self.embeddings.embed_query(query)]
        else:
            kw["query_texts"] = [query]
        res = col.query(**kw)
        ids = (res.get("ids") or [[None] * len(res["documents"][0])])[0]
        return [{"id": i, "text": d, "metadata": m or {}, "score": round(1 - dist, 3)}
                for i, d, m, dist in zip(ids, res["documents"][0], res["metadatas"][0], res["distances"][0])]

    def delete_by_document(self, doc_id):
        self._col().delete(where={"doc_id": doc_id})

    def delete(self):
        if self._drop is not None:
            self._drop()

    def update_metadata(self, patch, *, document=None, ids=None):
        col = self._col()
        if document is not None:
            got = col.get(where={"doc_id": document}, include=["metadatas"])
        elif ids:
            got = col.get(ids=ids, include=["metadatas"])
        else:
            raise ValueError("give a document or ids")
        if not got["ids"]:
            return 0
        col.update(ids=got["ids"], metadatas=[{**(m or {}), **patch} for m in got["metadatas"]])
        return len(got["ids"])

    def replace_metadata(self, ids, metadatas):
        if ids:
            self._col().update(ids=ids, metadatas=metadatas)

    def count(self):
        return self._col().count()

    def health(self):
        try:
            return True, f"{self._col().count()} chunks"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def stats(self):
        docs = set()
        n = 0
        for _, metas in self.iter_metadata():
            n += len(metas)
            docs.update((m or {}).get("doc_id") for m in metas)
        docs.discard(None)
        return {"chunks": n, "documents": len(docs)}

    def iter_metadata(self, batch=500):
        col = self._col()
        offset = 0
        while True:
            got = col.get(limit=batch, offset=offset, include=["metadatas"])
            if not got["ids"]:
                return
            yield got["ids"], got["metadatas"]
            offset += len(got["ids"])

    def iter_texts(self, batch=500):
        col = self._col()
        offset = 0
        while True:
            got = col.get(limit=batch, offset=offset, include=["documents", "metadatas"])
            if not got["ids"]:
                return
            yield got["ids"], got["documents"], [m or {} for m in got["metadatas"]]
            offset += len(got["ids"])


STORES = {"chroma": ChromaStore}
