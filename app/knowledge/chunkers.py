"""Chunking strategies behind one interface. Only the recursive fixed-size strategy exists today
(900 / 150 from Global Settings); a knowledge base may override size and overlap."""
from abc import ABC, abstractmethod

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app import global_settings


class Chunker(ABC):
    name: str

    @abstractmethod
    def split(self, text: str) -> list[str]: ...


class RecursiveChunker(Chunker):
    name = "recursive"

    def __init__(self, chunk_size: int, chunk_overlap: int):
        if chunk_overlap >= chunk_size:
            raise ValueError("Chunk overlap must be smaller than the chunk size.")
        self.size, self.overlap = chunk_size, chunk_overlap
        self._splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)

    def split(self, text: str) -> list[str]:
        return self._splitter.split_text(text)


REGISTRY = {"recursive": RecursiveChunker}


def effective(kb_size: int | None, kb_overlap: int | None, db=None) -> tuple[int, int]:
    """(size, overlap) for a knowledge base: its own override, else the Global Settings value."""
    gs = global_settings.current(db)
    return (kb_size if kb_size is not None else gs.chunk_size,
            kb_overlap if kb_overlap is not None else gs.chunk_overlap)


def for_kb(kb: dict | None, db=None) -> Chunker:
    size, overlap = effective((kb or {}).get("chunk_size"), (kb or {}).get("chunk_overlap"), db)
    return RecursiveChunker(size, overlap)
