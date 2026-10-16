"""Embedding models behind one interface (LangChain `Embeddings` shape: embed_documents / embed_query).

`chroma-default` is the model Chroma already uses for the existing index (all-MiniLM-L6-v2, 384 dimensions).
It is marked `builtin`: the store lets Chroma embed, so vectors are identical and nothing is re-indexed.
A non-builtin model is embedded by us and handed to the store as vectors.
"""
from abc import ABC, abstractmethod

from app.spec import Field


class EmbeddingModel(ABC):
    name: str
    title: str = ""
    builtin: bool = False
    cost_per_million_tokens: float | None = None      # USD; 0 = runs locally; None = unknown
    SPEC: tuple[Field, ...] = ()

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def test(self) -> tuple[bool, str]:
        """Embed a sample and report the vector size."""
        try:
            size = len(self.embed_query("connection test"))
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
        return True, f"Works; vectors have {size} dimensions."


class ChromaDefaultEmbeddings(EmbeddingModel):
    name = "chroma-default"
    title = "Local MiniLM (Chroma built-in)"
    builtin = True
    cost_per_million_tokens = 0.0

    def __init__(self):
        self._fn = None

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self._fn is None:
            from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
            self._fn = DefaultEmbeddingFunction()
        return [list(map(float, v)) for v in self._fn(texts)]


REGISTRY: dict[str, type[EmbeddingModel]] = {"chroma-default": ChromaDefaultEmbeddings}
DEFAULT = "chroma-default"


def get(name: str = DEFAULT) -> EmbeddingModel:
    try:
        return REGISTRY[name]()
    except KeyError:
        raise ValueError(f"unknown embedding model {name!r}") from None
