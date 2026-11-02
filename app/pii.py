"""Personal-data engine shared by the guardrails and document indexing.

* Admin-defined types: a regular expression, optional context words and a confidence, registered with the detector
  next to the built-in types (and usable wherever those are: questions, answers, documents).
* Sensitive keywords: when a listed keyword appears ("Salary: 12,40,000", "DOB 12/03/1990", a "Salary | 12,40,000"
  table row), the value that follows it is personal data, whatever its format.
* Tokens and the vault: in documents a value becomes `<EMAIL_ADDRESS_7f3a9c21>` instead of a plain label. The suffix
  is an HMAC of the value keyed per knowledge base, so the same value gets the same token in every chunk (the model can
  still tell that two passages mention the same person) while the token says nothing about the value. The real value
  is kept in `pii_vault`, encrypted with the master key, for the people allowed to see it.

The vault never holds a value in plain text and nothing here logs a value.
"""
import hashlib
import hmac
import logging
import re
import secrets
import threading
import time

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.db import Database, PiiCustomType, PiiVault, PiiVaultRef, Secret, get_db

log = logging.getLogger(__name__)
KEYWORD_SCORE = 0.85
CACHE_SECONDS = 2.0
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,39}$")
RESERVED = {"PERSON", "SENSITIVE_VALUE", "LOCATION", "DATE_TIME", "NRP", "ORGANIZATION"}
TOKEN_RE = re.compile(r"<(([A-Z][A-Z0-9_]*?)_[0-9a-f]{8,64})>")            # group 1: the token, group 2: its type
PLACEHOLDER_RE = re.compile(r"<[A-Z][A-Z0-9_]*(?:_[0-9a-f]{8,64})?>")    # a token or a plain label such as <EMAIL_ADDRESS>
MAX_VALUE = 120                       # a keyword's value never runs on for more than this many characters
_lock = threading.Lock()
_custom_cache: tuple[float, int, list] | None = None
_registered: dict[int, tuple] = {}    # id(engine) -> signature of the custom recognizers it holds
_token_key: tuple[int, bytes] | None = None


# ---- Admin-defined types -------------------------------------------------------------------------------------------
def _view(t: PiiCustomType) -> dict:
    return {"id": t.id, "name": t.name, "pattern": t.pattern, "context": list(t.context or []), "confidence": t.confidence,
            "updated_at": t.updated_at, "updated_by": t.updated_by}


def list_custom(db: Database | None = None) -> list[dict]:
    """The Admin-defined types (cached for two seconds; every detection reads them)."""
    global _custom_cache
    db = db or get_db()
    now = time.monotonic()
    with _lock:
        hit = _custom_cache
    if hit and hit[1] == id(db) and now - hit[0] < CACHE_SECONDS:
        return hit[2]
    with db.session() as s:
        rows = [_view(t) for t in s.scalars(select(PiiCustomType).order_by(PiiCustomType.name))]
    with _lock:
        _custom_cache = (now, id(db), rows)
    return rows


def _invalidate() -> None:
    global _custom_cache
    with _lock:
        _custom_cache = None


def check_custom(name: str, pattern: str, context, confidence) -> dict:
    """Validate an Admin-defined type. Raises ValueError with a message an Admin can act on."""
    from app.guardrails import ALLOWED_TYPES
    name = (name or "").strip().upper().replace(" ", "_")
    if not NAME_RE.match(name):
        raise ValueError("The name must be 2 to 40 capital letters, digits or underscores, starting with a letter (for example EMPLOYEE_ID).")
    if name in ALLOWED_TYPES or name in RESERVED:
        raise ValueError(f"{name} is a built-in type. Choose another name.")
    pattern = (pattern or "").strip()
    if not pattern or len(pattern) > 300:
        raise ValueError("The pattern must be between 1 and 300 characters.")
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise ValueError(f"The pattern is not a valid regular expression: {e}.") from None
    if rx.fullmatch(""):
        raise ValueError("The pattern matches empty text, so it would match everywhere.")
    if not isinstance(context, list) or not all(isinstance(c, str) for c in context):
        raise ValueError("Context words must be a list.")
    context = list(dict.fromkeys(c.strip().lower() for c in context if c.strip()))
    if len(context) > 20 or any(len(c) > 40 for c in context):
        raise ValueError("Up to 20 context words of up to 40 characters.")
    if isinstance(confidence, bool) or not isinstance(confidence, int) or not 10 <= confidence <= 100:
        raise ValueError("Confidence must be a whole number between 10 and 100.")
    return {"name": name, "pattern": pattern, "context": context, "confidence": confidence}


def save_custom(db: Database, data: dict, actor: str, type_id: int | None = None) -> dict:
    """Create or change an Admin-defined type. A new type is switched on for masking straight away."""
    clean = check_custom(data.get("name", ""), data.get("pattern", ""), data.get("context", []), data.get("confidence", 60))
    with db.session() as s:
        clash = s.scalar(select(PiiCustomType.id).where(PiiCustomType.name == clean["name"]))
        if clash and clash != type_id:
            raise LookupError(f"A type called {clean['name']} already exists.")
        if type_id is None:
            row = PiiCustomType(updated_by=actor, **clean)
            s.add(row)
        else:
            row = s.get(PiiCustomType, type_id)
            if row is None:
                raise ValueError("No such type.")
            old = row.name
            for k, v in clean.items():
                setattr(row, k, v)
            row.updated_by = actor
        s.flush()
        out = _view(row)
    _invalidate()
    from app import guardrails
    snap = guardrails.current(db)
    types = list(snap.masked_types)
    if type_id is not None and old != out["name"]:
        types = [out["name"] if t == old else t for t in types]
    elif type_id is None and out["name"] not in types:
        types.append(out["name"])
    if types != list(snap.masked_types):
        guardrails.update({"masked_types": types}, actor, db)
    return out


def delete_custom(db: Database, type_id: int, actor: str) -> dict:
    with db.session() as s:
        row = s.get(PiiCustomType, type_id)
        if row is None:
            raise ValueError("No such type.")
        out = _view(row)
        s.delete(row)
    _invalidate()
    from app import guardrails
    snap = guardrails.current(db)
    if out["name"] in snap.masked_types:
        guardrails.update({"masked_types": [t for t in snap.masked_types if t != out["name"]]}, actor, db)
    return out


def sync_recognizers(engine, db: Database | None = None) -> None:
    """Make the detector hold exactly the current Admin-defined types (cheap when nothing changed)."""
    try:
        rows = list_custom(db)
    except Exception:
        log.warning("could not read the custom personal-data types", exc_info=True)
        return
    sig = tuple((r["name"], r["pattern"], tuple(r["context"]), r["confidence"]) for r in rows)
    with _lock:
        if _registered.get(id(engine)) == sig:
            return
        from presidio_analyzer import Pattern, PatternRecognizer
        for rec in list(engine.registry.recognizers):
            if getattr(rec, "name", "").startswith("custom_"):
                engine.registry.remove_recognizer(rec.name)
        for name, pattern, context, confidence in sig:
            engine.registry.add_recognizer(PatternRecognizer(
                supported_entity=name, name=f"custom_{name}", patterns=[Pattern(name, pattern, confidence / 100)],
                context=list(context) or None))
        _registered[id(engine)] = sig


def try_pattern(pattern: str, text: str) -> list[str]:
    """What a pattern would match in a sample (for the Try it box; at most 20 matches)."""
    check_custom("TRY_IT", pattern, [], 60)
    return [m.group(0) for m in re.finditer(pattern, text or "")][:20]


# ---- sensitive keywords -------------------------------------------------------------------------------------------
_NUMBER = r"(?:[₹$€£]\s?)?\d[\d,./\-: ]*\d(?:\s?(?:lakhs?|crores?|k|L|INR|USD|EUR|GBP))?"
_LABEL = r"(?:\s*(?:no\.?|number|#))?"
_compiled: dict[tuple, list] = {}


def _patterns(keywords) -> list:
    key = tuple(sorted({k.strip().lower() for k in keywords if k.strip()}))
    if key not in _compiled:
        if len(_compiled) > 32:
            _compiled.clear()
        out = []
        for kw in key:
            words = r"\s+".join(re.escape(w) for w in kw.split())
            head = r"(?<!\w)" + words + r"(?!\w)" + _LABEL
            # "Salary: x", "Salary = x", "Salary | x" (a table row) or "Salary - x": any value up to the end of the sentence or cell
            out.append(re.compile(head + r"\s*(?:[:=|]|\s[-–—])\s*(?P<v>(?:[^\n;|.]|\.(?=\S))+)", re.I))
            # "Salary 12,40,000", "salary is 12 lakh", "DOB of 12/03/1990": a value that starts with a number
            out.append(re.compile(head + r"(?:\s+(?:is|was|are|were|of))?\s*(?P<v>" + _NUMBER + ")", re.I))
        _compiled[key] = out
    return _compiled[key]


def keyword_spans(text: str, keywords) -> list[tuple[int, int]]:
    """(start, end) of each value that follows a sensitive keyword."""
    if not keywords or not text:
        return []
    spans = []
    for rx in _patterns(keywords):
        for m in rx.finditer(text):
            start, end = m.span("v")
            value = text[start:end]
            end = start + len(value.rstrip())
            if end - start > MAX_VALUE:
                cut = text.rfind(" ", start, start + MAX_VALUE)
                end = cut if cut > start else start + MAX_VALUE
            if end > start and any(c.isalnum() for c in text[start:end]):
                spans.append((start, end))
    return spans


# ---- tokens and the vault ---------------------------------------------------------------------------------------
def _crypto():
    from app.secretstore import default_store
    store = default_store()
    if store is None:
        raise ValueError("Personal data cannot be stored: APP_SECRET_KEY is not set.")
    return store


def _key(db: Database) -> bytes:
    """The secret that keys the token HMAC (random, stored encrypted like any other credential, created on first use)."""
    global _token_key
    with _lock:
        if _token_key and _token_key[0] == id(db):
            return _token_key[1]
    crypto = _crypto()
    where = (Secret.owner_type == "pii", Secret.owner_id == "tokens", Secret.name == "hmac_key")
    with db.session() as s:
        row = s.scalar(select(Secret).where(*where))
        if row is None:
            try:
                s.add(Secret(owner_type="pii", owner_id="tokens", name="hmac_key", ciphertext=crypto.encrypt(secrets.token_hex(32))))
                s.flush()
            except IntegrityError:
                s.rollback()
            row = s.scalar(select(Secret).where(*where))
        key = bytes.fromhex(crypto.decrypt(row.ciphertext))
    with _lock:
        _token_key = (id(db), key)
    return key


def _normalise(value: str) -> str:
    return " ".join(value.split()).casefold()


def _vault_token(db: Database, kb_id: int, entity: str, value: str, doc_id: int | None) -> str:
    digest = hmac.new(_key(db), f"{kb_id}\x00{entity}\x00{_normalise(value)}".encode(), hashlib.sha256).hexdigest()
    for _ in range(3):
        with db.session() as s:
            row = s.scalar(select(PiiVault).where(PiiVault.kb_id == kb_id, PiiVault.value_hash == digest))
            if row is None:
                taken = set(s.scalars(select(PiiVault.token).where(PiiVault.kb_id == kb_id,
                                                                     PiiVault.token.like(f"{entity}_{digest[:8]}%"))))
                token = next(f"{entity}_{digest[:n]}" for n in (8, 12, 16, 64) if f"{entity}_{digest[:n]}" not in taken)
                row = PiiVault(kb_id=kb_id, token=token, entity_type=entity, value_hash=digest,
                               value_encrypted=_crypto().encrypt(value))
                try:
                    s.add(row)
                    s.flush()
                except IntegrityError:                 # another worker stored the same value a moment ago: use theirs
                    s.rollback()
                    continue
            if doc_id is not None and s.get(PiiVaultRef, (row.id, doc_id)) is None:
                s.add(PiiVaultRef(vault_id=row.id, doc_id=doc_id))
            return row.token
    raise RuntimeError("could not store a personal-data value")


def tokenise(text: str, kb_id: int, doc_id: int | None, hits, db: Database | None = None) -> tuple[str, dict]:
    """Replace each hit (from guardrails.detect) with its vault token. Returns (new text, {type: how many})."""
    db = db or get_db()
    counts: dict = {}
    for h in sorted(hits, key=lambda h: h.start, reverse=True):
        token = _vault_token(db, kb_id, h.entity_type, text[h.start:h.end], doc_id)
        text = text[:h.start] + f"<{token}>" + text[h.end:]
        counts[h.entity_type] = counts.get(h.entity_type, 0) + 1
    return text, counts


def reveal(text: str, kb_ids, db: Database | None = None) -> tuple[str, int]:
    """Put the real values back for tokens of the given knowledge bases. Unknown tokens are left as they are.
    Callers decide who may see the values; this function does not."""
    found = {m.group(1) for m in TOKEN_RE.finditer(text or "")}
    if not found or not kb_ids:
        return text, 0
    db = db or get_db()
    crypto = _crypto()
    with db.session() as s:
        rows = s.execute(select(PiiVault.token, PiiVault.value_encrypted)
                         .where(PiiVault.kb_id.in_(list(kb_ids)), PiiVault.token.in_(found))).all()
    values = {t: crypto.decrypt(v) for t, v in rows}
    n = 0

    def put(m):
        nonlocal n
        if m.group(1) in values:
            n += 1
            return values[m.group(1)]
        return m.group(0)
    return TOKEN_RE.sub(put, text), n


def label(text: str) -> str:
    """Tokens shown as plain labels (<EMAIL_ADDRESS_7f3a9c21> -> <EMAIL_ADDRESS>), for people who may not see the values."""
    return TOKEN_RE.sub(lambda m: f"<{m.group(2)}>", text or "")


def label_stream(parts):
    """`label` for an answer that arrives in pieces: a token split across pieces is held back until it is complete."""
    held = ""
    for part in parts:
        held += part
        cut = held.rfind("<")
        if cut >= 0 and ">" not in held[cut:] and len(held) - cut <= 90:
            ready, held = held[:cut], held[cut:]
        else:
            ready, held = held, ""
        if ready:
            yield label(ready)
    if held:
        yield label(held)


def release_document(db: Database, doc_id: int) -> int:
    """Forget which tokens a document used; values no document uses any more are deleted. Returns how many values went."""
    with db.session() as s:
        kb_ids = set(s.scalars(select(PiiVault.kb_id).join(PiiVaultRef, PiiVaultRef.vault_id == PiiVault.id)
                               .where(PiiVaultRef.doc_id == doc_id)))
        s.execute(delete(PiiVaultRef).where(PiiVaultRef.doc_id == doc_id))
        if not kb_ids:
            return 0
        used = select(PiiVaultRef.vault_id)
        orphans = list(s.scalars(select(PiiVault.id).where(PiiVault.kb_id.in_(kb_ids), PiiVault.id.not_in(used))))
        if orphans:
            s.execute(delete(PiiVault).where(PiiVault.id.in_(orphans)))
        return len(orphans)


def release_kb(db: Database, kb_id: int) -> int:
    """Delete every value of a knowledge base (it is being deleted)."""
    with db.session() as s:
        ids = select(PiiVault.id).where(PiiVault.kb_id == kb_id)
        s.execute(delete(PiiVaultRef).where(PiiVaultRef.vault_id.in_(ids)))
        n = s.scalar(select(func.count()).select_from(PiiVault).where(PiiVault.kb_id == kb_id)) or 0
        s.execute(delete(PiiVault).where(PiiVault.kb_id == kb_id))
        return n


def counts_for_document(db: Database, doc_id: int) -> dict:
    """{type: distinct values} a document holds in the vault (never the values)."""
    with db.session() as s:
        rows = s.execute(select(PiiVault.entity_type, func.count()).join(PiiVaultRef, PiiVaultRef.vault_id == PiiVault.id)
                         .where(PiiVaultRef.doc_id == doc_id).group_by(PiiVault.entity_type)).all()
    return {t: n for t, n in rows}
