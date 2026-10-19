"""The metadata every stored chunk carries. Defined now, including who may see it, so permissions
(Phase 4) can be applied by updating metadata in place, with no re-embedding.

Chroma metadata values must be plain strings, numbers or booleans, so the allowed groups and people
are stored as "|1|2|" (empty string = everyone). Document ids are the file's path inside the KB.
"""
REQUIRED_KEYS = ("source", "doc_id", "page", "heading_path", "chunk", "kb_id", "department",
                 "allowed_groups", "allowed_people")


def encode_ids(ids) -> str:
    ids = sorted({int(i) for i in ids or ()})
    return "|" + "|".join(map(str, ids)) + "|" if ids else ""


def decode_ids(value: str) -> list[int]:
    return [int(x) for x in (value or "").split("|") if x]


def build(*, source: str, doc_id: str, page: int, chunk: int, kb_id: int, department: str,
          heading_path: str = "", allowed_groups=(), allowed_people=()) -> dict:
    return {"source": source, "doc_id": doc_id, "page": int(page or 0), "heading_path": heading_path,
            "chunk": int(chunk), "kb_id": int(kb_id), "department": department or "",
            "allowed_groups": encode_ids(allowed_groups), "allowed_people": encode_ids(allowed_people)}


def missing(meta: dict) -> list[str]:
    """Required keys absent from a chunk's metadata (empty list = complete)."""
    return [k for k in REQUIRED_KEYS if k not in meta]
