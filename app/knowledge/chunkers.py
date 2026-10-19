"""Chunking strategies behind one interface: `chunker.chunk(path) -> [Chunk(text, page, heading_path)]`.

* recursive  (fixed size): the original behaviour, unchanged. Page text is cut into pieces of at most `size` characters with `overlap`.
* per_page   one chunk per PDF page or slide (page numbers kept); a long page is split into pieces of at most `size`.
* by_heading sections of a Markdown, Word or PowerPoint file; each chunk carries its heading path ("Leave > Sick leave") and starts with it.
             Tiny sections are merged into the next one, oversized ones are split. A file with no headings (a PDF) falls back to pages.
* semantic   cuts where the meaning changes, measured with the knowledge base's embedding model. Slower and, with a paid model, costs more.

The structure-aware strategies keep a TABLE or a CODE BLOCK whole in one chunk (split by rows or lines only when it is huge, more than
4 x `size`; the header row repeats in every piece). A knowledge base's strategy is part of its index version: changing it builds a new index.
"""
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app import global_settings
from app.knowledge import parsers

MIN_CHARS = 120              # a section smaller than this is merged into the next one
HUGE_FACTOR = 4              # tables and code blocks bigger than this x size are split


@dataclass(frozen=True)
class Chunk:
    text: str
    page: int = 0
    heading_path: str = ""


STRATEGIES = {
    "recursive": {"title": "Fixed size", "help": "Cuts text into pieces of a set size with some overlap. Simple and predictable."},
    "per_page": {"title": "Per page or slide", "help": "One chunk per PDF page or slide, keeping page numbers; a long page is split."},
    "by_heading": {"title": "By heading", "help": "Follows the headings of Markdown, Word and PowerPoint files and records the heading path on each chunk. Files without headings (PDFs) fall back to pages."},
    "semantic": {"title": "Semantic", "help": "Cuts where the meaning changes. Slower to build, and costs more with a paid embedding model."},
}
DEFAULT_STRATEGY = "recursive"


class Chunker(ABC):
    name: str
    size: int
    overlap: int

    @abstractmethod
    def chunk(self, path: Path) -> list[Chunk]: ...

    def split(self, text: str) -> list[str]:
        """Cut a plain text (used by callers that only have text)."""
        return [c.text for c in self.chunk_text(text)]

    def chunk_text(self, text: str) -> list[Chunk]:
        return [Chunk(t) for t in _splitter(self.size, self.overlap).split_text(text)]


def _splitter(size: int, overlap: int):
    return RecursiveCharacterTextSplitter(chunk_size=size, chunk_overlap=overlap)


def _check(size: int, overlap: int) -> None:
    if overlap >= size:
        raise ValueError("Chunk overlap must be smaller than the chunk size.")


class RecursiveChunker(Chunker):
    name = "recursive"

    def __init__(self, chunk_size: int, chunk_overlap: int):
        _check(chunk_size, chunk_overlap)
        self.size, self.overlap = chunk_size, chunk_overlap
        self._splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)

    def split(self, text: str) -> list[str]:
        return self._splitter.split_text(text)

    def chunk(self, path: Path) -> list[Chunk]:
        out = []
        for page, text in parsers.load_pages(path):
            out += [Chunk(t, page) for t in self.split(text) if t.strip()]
        return out


# ---- shared helpers for the structure-aware strategies ------------------------------------------------------------------
def _split_huge(block: parsers.Block, size: int) -> list[str]:
    """A table or code block that is too big: split by rows / lines, repeating a table's header row in every piece."""
    lines = block.text.split("\n")
    header = lines[0] if block.kind == "table" and len(lines) > 1 else ""
    pieces, cur = [], []
    for line in lines:
        if cur and sum(len(x) + 1 for x in cur) + len(line) > size:
            pieces.append("\n".join(cur))
            cur = [header] if header and line != header else []
        cur.append(line)
    if cur:
        pieces.append("\n".join(cur))
    return pieces


def _pieces(block: parsers.Block, size: int, overlap: int) -> list[str]:
    """The text pieces one block becomes: whole if it fits, otherwise split (tables and code only when huge)."""
    if len(block.text) <= size:
        return [block.text]
    if block.kind in ("table", "code"):
        return [block.text] if len(block.text) <= size * HUGE_FACTOR else _split_huge(block, size)
    return [t for t in _splitter(size, overlap).split_text(block.text) if t.strip()]


def _pack(blocks: list[parsers.Block], size: int, overlap: int, prefix: str = "") -> list[tuple[int, str]]:
    """Greedily pack blocks into chunks of at most `size` characters. Returns [(page of the first block, text)].
    A table or code block that does not fit with what is already there starts a new chunk, so it is never cut."""
    out: list[tuple[int, str]] = []
    cur: list[str] = []
    cur_page = 0
    cur_len = len(prefix)

    def flush():
        nonlocal cur, cur_len, cur_page
        if cur:
            out.append((cur_page, (prefix + "\n" if prefix else "") + "\n\n".join(cur)))
        cur, cur_len, cur_page = [], len(prefix), 0

    for b in blocks:
        for piece in _pieces(b, size, overlap):
            if cur and cur_len + len(piece) + 2 > size:
                flush()
            if not cur:
                cur_page = b.page
            cur.append(piece)
            cur_len += len(piece) + 2
    flush()
    return out


class PerPageChunker(Chunker):
    name = "per_page"

    def __init__(self, chunk_size: int, chunk_overlap: int):
        _check(chunk_size, chunk_overlap)
        self.size, self.overlap = chunk_size, chunk_overlap

    def chunk(self, path: Path) -> list[Chunk]:
        blocks = [b for b in parsers.load_blocks(path) if b.text.strip()]
        out: list[Chunk] = []
        pages = sorted({b.page for b in blocks})
        for page in pages:
            mine = [b for b in blocks if b.page == page]
            out += [Chunk(t, page) for _, t in _pack(mine, self.size, self.overlap)]
        return out


class HeadingChunker(Chunker):
    name = "by_heading"

    def __init__(self, chunk_size: int, chunk_overlap: int):
        _check(chunk_size, chunk_overlap)
        self.size, self.overlap = chunk_size, chunk_overlap

    def chunk(self, path: Path) -> list[Chunk]:
        blocks = [b for b in parsers.load_blocks(path) if b.text.strip()]
        if not any(b.kind == "heading" for b in blocks):
            return PerPageChunker(self.size, self.overlap).chunk(path)       # no headings in this file: use its pages
        sections: list[dict] = []
        stack: list[tuple[int, str]] = []
        cur = {"path": "", "page": 0, "blocks": []}
        for b in blocks:
            if b.kind == "heading":
                if cur["blocks"]:
                    sections.append(cur)
                while stack and stack[-1][0] >= b.level:
                    stack.pop()
                stack.append((b.level, b.text))
                cur = {"path": " > ".join(t for _, t in stack), "page": b.page, "blocks": []}
            else:
                if not cur["blocks"] and not cur["page"]:
                    cur["page"] = b.page
                cur["blocks"].append(b)
        if cur["blocks"]:
            sections.append(cur)
        # A section with only a line or two of text is not worth a chunk of its own: fold it into the section before it (or, if it is
        # the first, the one after it). Never across pages or slides, so a slide keeps its own chunk and page number.
        def tiny(sec):
            return sum(len(b.text) for b in sec["blocks"]) < MIN_CHARS

        def same_page(a, b):
            return a["page"] == b["page"] or not a["page"] or not b["page"]

        def fold(into, sec):
            label = sec["path"].split(" > ")[-1] if sec["path"] else ""
            into["blocks"] = into["blocks"] + ([parsers.Block(sec["page"], "text", label)] if label else []) + sec["blocks"]

        merged: list[dict] = []
        for sec in sections:
            if tiny(sec) and merged and same_page(merged[-1], sec):
                fold(merged[-1], sec)
            else:
                merged.append(sec)
        if len(merged) > 1 and tiny(merged[0]) and same_page(merged[0], merged[1]):
            first = merged.pop(0)
            label = first["path"].split(" > ")[-1] if first["path"] else ""
            merged[0] = {**merged[0], "page": first["page"] or merged[0]["page"],
                         "blocks": ([parsers.Block(first["page"], "text", label)] if label else []) + first["blocks"] + merged[0]["blocks"]}
        out: list[Chunk] = []
        for sec in merged:
            for page, text in _pack(sec["blocks"], self.size, self.overlap, prefix=sec["path"]):
                out.append(Chunk(text, page or sec["page"], sec["path"]))
        return out


class SemanticChunker(Chunker):
    name = "semantic"
    BREAK_PERCENTILE = 90

    def __init__(self, chunk_size: int, chunk_overlap: int, embedder=None):
        _check(chunk_size, chunk_overlap)
        self.size, self.overlap, self._embedder = chunk_size, chunk_overlap, embedder

    def _embed(self, texts: list[str]):
        if self._embedder is None:
            from app.knowledge import embeddings
            self._embedder = embeddings.get()
        return self._embedder.embed_documents(texts)

    def chunk(self, path: Path) -> list[Chunk]:
        import numpy as np
        blocks = [b for b in parsers.load_blocks(path) if b.text.strip()]
        units: list[tuple[int, str, str]] = []                       # (page, text, kind); a table or code block is one unit
        for b in blocks:
            if b.kind in ("table", "code"):
                units += [(b.page, t, b.kind) for t in _pieces(b, self.size, self.overlap)]
            else:
                sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n", b.text) if s.strip()]
                units += [(b.page, s, "text") for s in sentences]
        if not units:
            return []
        vectors = np.array(self._embed([u[1] for u in units]), dtype=float)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors = vectors / np.where(norms == 0, 1, norms)
        dist = 1 - (vectors[:-1] * vectors[1:]).sum(axis=1) if len(units) > 1 else np.array([])
        threshold = float(np.percentile(dist, self.BREAK_PERCENTILE)) if len(dist) else 1.0
        out: list[Chunk] = []
        cur: list[str] = []
        cur_page = units[0][0]
        for i, (page, text, kind) in enumerate(units):
            if cur and (len("\n".join(cur)) + len(text) + 1 > self.size or kind != "text" or units[i - 1][2] != "text"):
                out.append(Chunk("\n".join(cur), cur_page)); cur, cur_page = [], page
            if not cur:
                cur_page = page
            cur.append(text)
            if i < len(dist) and dist[i] > threshold and len("\n".join(cur)) >= MIN_CHARS:
                out.append(Chunk("\n".join(cur), cur_page)); cur = []
        if cur:
            out.append(Chunk("\n".join(cur), cur_page))
        return out


_BUILDERS = {"recursive": RecursiveChunker, "per_page": PerPageChunker, "by_heading": HeadingChunker, "semantic": SemanticChunker}
REGISTRY = _BUILDERS


def build(strategy: str, size: int, overlap: int, embedder=None) -> Chunker:
    if strategy not in _BUILDERS:
        raise ValueError(f"Unknown chunking strategy {strategy!r}.")
    return _BUILDERS[strategy](size, overlap, embedder) if strategy == "semantic" else _BUILDERS[strategy](size, overlap)


def effective(kb_size: int | None, kb_overlap: int | None, db=None) -> tuple[int, int]:
    """(size, overlap) for a knowledge base: its own override, else the Global Settings value."""
    gs = global_settings.current(db)
    return (kb_size if kb_size is not None else gs.chunk_size,
            kb_overlap if kb_overlap is not None else gs.chunk_overlap)


def for_kb(kb: dict | None, db=None) -> Chunker:
    size, overlap = effective((kb or {}).get("chunk_size"), (kb or {}).get("chunk_overlap"), db)
    return build((kb or {}).get("chunk_strategy") or DEFAULT_STRATEGY, size, overlap)
