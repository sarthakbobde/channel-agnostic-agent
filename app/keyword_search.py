"""Keyword (BM25) search over a knowledge base's passages, and the helpers the retrieval strategies share.

The index is built in memory from the passages the active vector index already holds, so it works for every store
(Chroma, pgvector, Azure AI Search) and needs no extra service. It is cached per knowledge base and index version and
rebuilt when the passages change. Who may see what is decided OUTSIDE this module: a search only ever scores passages
whose document is on the allow-list it is given, and the statistics (document counts, average length) are taken from
those passages alone, so a restricted document cannot influence the ranking of what someone else may see.
"""
import logging
import math
import re
import threading
import time
import unicodedata
from collections import Counter, defaultdict

log = logging.getLogger(__name__)

K1, B = 1.5, 0.75
CACHE_SECONDS = 15.0              # passage text is cached this long; a new index version or passage count rebuilds at once
SATURATION = 3.0                  # score = bm25 / (bm25 + SATURATION): a strong match is about 0.5 or more, a weak one under 0.3
STOPWORDS = frozenset("a an the of to in on at for from by with and or is are was were be been it its this that these those as do does did "
                      "how what when where which who whom why can could should would will shall may might i me my we our you your they their "
                      "there here about into over under per any all".split())


def _marks() -> str:
    """Every combining mark in the Basic Multilingual Plane, as a character class body. Python's word class leaves them
    out, which splits words in Indic and other scripts (Hindi vowel signs and viramas are marks) into single letters."""
    out, start, prev = [], None, None
    for cp in range(0x10000):
        if unicodedata.category(chr(cp)).startswith("M") and not 0xD800 <= cp <= 0xDFFF:
            if start is None:
                start = cp
            elif cp != prev + 1:
                out.append((start, prev))
                start = cp
            prev = cp
    out.append((start, prev))
    return "".join(f"{re.escape(chr(a))}-{re.escape(chr(b))}" for a, b in out)


_WORD = re.compile(r"(?:\w|[" + _marks() + "])+", re.UNICODE)


def tokens(text: str) -> list[str]:
    """Lower-case words without common filler; a trailing plural 's'/'es' is dropped so 'days' matches 'day'."""
    out = []
    for w in _WORD.findall((text or "").lower().replace("_", " ")):
        if w in STOPWORDS:
            continue
        if len(w) > 4 and w.endswith("es") and not w.endswith(("ses", "xes")):
            w = w[:-2]
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


class Index:
    def __init__(self):
        self.ids: list[str] = []
        self.texts: list[str] = []
        self.metas: list[dict] = []
        self.doc_of: list[str] = []
        self.lens: list[int] = []
        self.postings: dict[str, dict[int, int]] = defaultdict(dict)
        self.per_doc: dict[str, list[int]] = defaultdict(lambda: [0, 0])      # doc_id -> [passages, total length]

    def add(self, id_: str, text: str, meta: dict) -> None:
        i = len(self.ids)
        toks = tokens(text)
        self.ids.append(id_)
        self.texts.append(text)
        self.metas.append(meta or {})
        doc = str((meta or {}).get("doc_id", ""))
        self.doc_of.append(doc)
        self.lens.append(len(toks))
        for t, n in Counter(toks).items():
            self.postings[t][i] = n
        agg = self.per_doc[doc]
        agg[0] += 1
        agg[1] += len(toks)

    def search(self, query: str, k: int, allowed_docs) -> list[dict]:
        """-> [{id, text, metadata, raw, score}], best first. Only passages of `allowed_docs` are considered."""
        allowed = {str(d) for d in allowed_docs}
        terms = list(dict.fromkeys(tokens(query)))
        n = sum(v[0] for d, v in self.per_doc.items() if d in allowed)
        if not terms or not n:
            return []
        avg = (sum(v[1] for d, v in self.per_doc.items() if d in allowed) / n) or 1.0
        scores: dict[int, float] = defaultdict(float)
        for t in terms:
            post = {i: tf for i, tf in self.postings.get(t, {}).items() if self.doc_of[i] in allowed}
            if not post:
                continue
            idf = math.log(1 + (n - len(post) + 0.5) / (len(post) + 0.5))
            for i, tf in post.items():
                scores[i] += idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * self.lens[i] / avg))
        top = sorted(scores.items(), key=lambda kv: -kv[1])[:k]
        return [{"id": self.ids[i], "text": self.texts[i], "metadata": self.metas[i], "raw": round(s, 3), "score": round(s / (s + SATURATION), 3)}
                for i, s in top]


_cache: dict[tuple, tuple[float, Index]] = {}
_lock = threading.Lock()


def _build(store) -> Index:
    idx = Index()
    for ids, texts, metas in store.iter_texts():
        for i, t, m in zip(ids, texts, metas):
            idx.add(i, t, m)
    return idx


def index_for(kb_id: int, version: int | None, store) -> Index:
    """The cached keyword index of the active passages (rebuilt when the version or the passage count changes, or after a short time)."""
    count = store.count()
    key = (kb_id, version, count)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    idx = _build(store)
    with _lock:
        for old in [k for k in _cache if k[0] == kb_id]:
            _cache.pop(old, None)
        _cache[key] = (now, idx)
    return idx


def clear() -> None:
    with _lock:
        _cache.clear()


def overlap(a: str, b: str) -> float:
    """Word overlap between two passages (0..1), used to avoid sending near-duplicates (MMR)."""
    x, y = set(tokens(a)), set(tokens(b))
    return len(x & y) / len(x | y) if x and y else 0.0
