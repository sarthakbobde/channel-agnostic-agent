"""Glossaries of acronyms, synonyms and preferred terms (D22), per knowledge base or department.

A glossary fixes vocabulary mismatch ("PTO" in a question, "paid time off" in the policy) without changing documents or
the wording of answers. It changes only the search text: the question in the prompt, the memory and the answer stay as
the person wrote them, and the asker's allow-list applies exactly as before.

Versions work like prompts: an editable Draft, one Published version, and Archived ones that can be rolled back to. A
knowledge base's terms win over its department's (the department that owns the knowledge base). `glossary_mode` (a scoped
setting, off by default) chooses:
  * query: the published terms' other forms are appended to the search text, which feeds both the vector and the keyword
    search. The Phase 3 comparison chose one combined string over two searches merged by rank;
  * index: terms marked stable are also added to each passage when it is embedded (the stored and displayed text is the
    original); the other terms are still expanded at query time. A change to the stable terms flags a re-index.
Matching is on whole words, multi-word terms included, with the keyword search's word rules (D35). A term with a language
only applies to questions (or passages) in that language.
"""
import csv
import hashlib
import io
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import delete, func, select

from app import global_settings, language, scoped_settings
from app.db import Database, Glossary, GlossaryTerm, GlossaryVersion, get_db
from app.keyword_search import _WORD

log = logging.getLogger(__name__)
KB, DEPARTMENT = "kb", "department"
SCOPES = (KB, DEPARTMENT)
KINDS = ("acronym", "synonym", "preferred")
DRAFT, PUBLISHED, ARCHIVED = "Draft", "Published", "Archived"
MAX_TERMS = 2000
MAX_ALIASES = 20
CACHE_SECONDS = 2.0
CSV_FIELDS = ("canonical", "aliases", "kind", "language", "case_sensitive", "enabled", "stable")

_cache: dict = {}
_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def invalidate() -> None:
    with _lock:
        _cache.clear()


# ---- words -------------------------------------------------------------------------------------------------------------
def _fold(word: str) -> str:
    """Lower case and a simple plural: "leaves" and "leave", "boxes" and "box", "days" and "day" match."""
    w = word.lower()
    if len(w) > 4 and w.endswith("es") and w[:-2].endswith(("s", "x", "z", "ch", "sh")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def words(text: str, case_sensitive: bool = False) -> list[str]:
    """Whole words with the keyword search's rules (combining marks kept). Case and plurals are folded unless case matters."""
    found = _WORD.findall((text or "").replace("_", " "))
    return found if case_sensitive else [_fold(w) for w in found]


def _contains(hay: list[str], needle: list[str]) -> bool:
    n = len(needle)
    return n > 0 and any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))


# ---- terms ---------------------------------------------------------------------------------------------------------------
@dataclass
class Term:
    canonical: str
    aliases: list = field(default_factory=list)
    kind: str = "synonym"
    language: str = ""
    case_sensitive: bool = False
    enabled: bool = True
    stable: bool = False
    id: int | None = None
    source: str = ""                   # kb | department, when resolved

    def forms(self) -> list[str]:
        return [self.canonical] + list(self.aliases)

    def as_dict(self) -> dict:
        return {"id": self.id, "canonical": self.canonical, "aliases": list(self.aliases), "kind": self.kind, "language": self.language,
                "case_sensitive": self.case_sensitive, "enabled": self.enabled, "stable": self.stable}


def _term(r: GlossaryTerm, source: str = "") -> Term:
    return Term(r.canonical, list(r.aliases or []), r.kind, r.language or "", r.case_sensitive, r.enabled, r.stable, r.id, source)


def clean_terms(raw: list) -> list[Term]:
    """Validate a list of terms (dicts). Raises ValueError with a message an Editor can act on."""
    if not isinstance(raw, list):
        raise ValueError("Send a list of terms.")
    if len(raw) > MAX_TERMS:
        raise ValueError(f"A glossary can hold up to {MAX_TERMS} terms.")
    out = []
    for n, t in enumerate(raw, 1):
        if not isinstance(t, dict):
            raise ValueError(f"Term {n} is not readable.")
        canonical = " ".join(str(t.get("canonical") or "").split())
        if not canonical or len(canonical) > 200:
            raise ValueError(f"Term {n}: the term must be between 1 and 200 characters.")
        aliases = t.get("aliases") or []
        if isinstance(aliases, str):
            aliases = aliases.split("|")
        aliases = list(dict.fromkeys(" ".join(str(a).split()) for a in aliases if str(a).strip()))
        if not aliases:
            raise ValueError(f"'{canonical}': give at least one other form (an alias).")
        if len(aliases) > MAX_ALIASES or any(len(a) > 200 for a in aliases):
            raise ValueError(f"'{canonical}': up to {MAX_ALIASES} aliases of up to 200 characters.")
        kind = t.get("kind") or "synonym"
        if kind not in KINDS:
            raise ValueError(f"'{canonical}': the type must be one of {', '.join(KINDS)}.")
        lang = (t.get("language") or "").strip().lower()
        if lang and not language.valid(lang):
            raise ValueError(f"'{canonical}': '{lang}' is not a language code.")
        flags = {}
        for f, default in (("case_sensitive", False), ("enabled", True), ("stable", False)):
            v = t.get(f, default)
            if isinstance(v, str):
                v = v.strip().lower() in ("1", "true", "yes", "y")
            flags[f] = bool(v)
        if not words(canonical) or not all(words(a) for a in aliases):
            raise ValueError(f"'{canonical}': every form needs at least one letter or digit.")
        out.append(Term(canonical, aliases, kind, lang, **flags))
    return out


def conflicts(terms: list[Term]) -> list[dict]:
    """Problems that make expansion ambiguous: the same alias on two terms, an alias that is itself a term, a repeated term."""
    out, seen_alias, canon = [], {}, {}
    for t in terms:
        key = " ".join(words(t.canonical))
        if key in canon:
            out.append({"kind": "repeated_term", "form": t.canonical, "terms": [canon[key], t.canonical]})
        canon.setdefault(key, t.canonical)
    for t in terms:
        for a in t.aliases:
            key = " ".join(words(a))
            if key in canon and canon[key] != t.canonical:
                out.append({"kind": "alias_is_a_term", "form": a, "terms": [t.canonical, canon[key]]})
            if key in seen_alias and seen_alias[key] != t.canonical:
                out.append({"kind": "alias_on_two_terms", "form": a, "terms": [seen_alias[key], t.canonical]})
            seen_alias.setdefault(key, t.canonical)
    return out


# ---- versions (the prompts pattern) --------------------------------------------------------------------------------------
def _glossary(s, scope_type: str, scope_id: int, actor: str = "", create: bool = False) -> Glossary | None:
    if scope_type not in SCOPES:
        raise ValueError("A glossary belongs to a knowledge base or a department.")
    g = s.scalar(select(Glossary).where(Glossary.scope_type == scope_type, Glossary.scope_id == int(scope_id)))
    if g is None and create:
        g = Glossary(scope_type=scope_type, scope_id=int(scope_id), created_by=actor)
        s.add(g)
        s.flush()
    return g


def _vview(v: GlossaryVersion, count: int) -> dict:
    return {"id": v.id, "version": v.version, "status": v.status, "created_by": v.created_by, "created_at": v.created_at,
            "published_by": v.published_by, "published_at": v.published_at, "terms": count}


def view(scope_type: str, scope_id: int, db: Database | None = None) -> dict:
    """Versions, newest first, and the terms of the Draft (if any) and of the Published version."""
    db = db or get_db()
    with db.session() as s:
        g = _glossary(s, scope_type, scope_id)
        if g is None:
            return {"versions": [], "draft": None, "published": None}
        versions = list(s.scalars(select(GlossaryVersion).where(GlossaryVersion.glossary_id == g.id).order_by(GlossaryVersion.version.desc())))
        counts = dict(s.execute(select(GlossaryTerm.version_id, func.count()).where(GlossaryTerm.version_id.in_([v.id for v in versions]))
                                .group_by(GlossaryTerm.version_id)).all()) if versions else {}

        def terms(v):
            return [_term(r).as_dict() for r in s.scalars(select(GlossaryTerm).where(GlossaryTerm.version_id == v.id).order_by(GlossaryTerm.canonical))]
        draft = next((v for v in versions if v.status == DRAFT), None)
        pub = next((v for v in versions if v.status == PUBLISHED), None)
        return {"versions": [_vview(v, counts.get(v.id, 0)) for v in versions],
                "draft": {**_vview(draft, counts.get(draft.id, 0)), "terms": terms(draft)} if draft else None,
                "published": {**_vview(pub, counts.get(pub.id, 0)), "terms": terms(pub)} if pub else None}


def version_terms(version_id: int, db: Database | None = None) -> list[Term]:
    with (db or get_db()).session() as s:
        return [_term(r) for r in s.scalars(select(GlossaryTerm).where(GlossaryTerm.version_id == version_id).order_by(GlossaryTerm.canonical))]


def version_scope(version_id: int, db: Database | None = None) -> tuple[str, int, dict] | None:
    """(scope type, scope id, the version) of a version id."""
    with (db or get_db()).session() as s:
        v = s.get(GlossaryVersion, version_id)
        g = s.get(Glossary, v.glossary_id) if v else None
        return (g.scope_type, g.scope_id, _vview(v, 0)) if g else None


def start_draft(scope_type: str, scope_id: int, actor: str, db: Database | None = None) -> dict:
    """The Draft to edit: the existing one, or a new one copied from the Published version."""
    db = db or get_db()
    with db.session() as s:
        g = _glossary(s, scope_type, scope_id, actor, create=True)
        draft = s.scalar(select(GlossaryVersion).where(GlossaryVersion.glossary_id == g.id, GlossaryVersion.status == DRAFT))
        if draft is None:
            number = (s.scalar(select(func.max(GlossaryVersion.version)).where(GlossaryVersion.glossary_id == g.id)) or 0) + 1
            draft = GlossaryVersion(glossary_id=g.id, version=number, status=DRAFT, created_by=actor)
            s.add(draft)
            s.flush()
            pub = s.scalar(select(GlossaryVersion).where(GlossaryVersion.glossary_id == g.id, GlossaryVersion.status == PUBLISHED))
            if pub is not None:
                for r in s.scalars(select(GlossaryTerm).where(GlossaryTerm.version_id == pub.id)):
                    s.add(GlossaryTerm(version_id=draft.id, canonical=r.canonical, aliases=list(r.aliases or []), kind=r.kind,
                                       language=r.language, case_sensitive=r.case_sensitive, enabled=r.enabled, stable=r.stable))
        return _vview(draft, 0)


def save_draft(scope_type: str, scope_id: int, raw_terms: list, actor: str, db: Database | None = None) -> tuple[int, int]:
    """Replace the Draft's terms. Returns (terms before, terms now). The Published version is untouched."""
    db = db or get_db()
    terms = clean_terms(raw_terms)
    draft = start_draft(scope_type, scope_id, actor, db)
    with db.session() as s:
        before = s.scalar(select(func.count()).select_from(GlossaryTerm).where(GlossaryTerm.version_id == draft["id"]))
        s.execute(delete(GlossaryTerm).where(GlossaryTerm.version_id == draft["id"]))
        for t in terms:
            s.add(GlossaryTerm(version_id=draft["id"], canonical=t.canonical, aliases=t.aliases, kind=t.kind, language=t.language,
                               case_sensitive=t.case_sensitive, enabled=t.enabled, stable=t.stable))
    return before, len(terms)


def discard_draft(scope_type: str, scope_id: int, db: Database | None = None) -> bool:
    db = db or get_db()
    with db.session() as s:
        g = _glossary(s, scope_type, scope_id)
        draft = g and s.scalar(select(GlossaryVersion).where(GlossaryVersion.glossary_id == g.id, GlossaryVersion.status == DRAFT))
        if not draft:
            return False
        s.execute(delete(GlossaryTerm).where(GlossaryTerm.version_id == draft.id))
        s.delete(draft)
    return True


def _activate(db: Database, version_id: int, actor: str, expect: str) -> tuple[dict, dict | None]:
    with db.session() as s:
        v = s.get(GlossaryVersion, version_id)
        if v is None:
            raise ValueError("No such glossary version.")
        if v.status != expect:
            raise ValueError("Only a Draft can be published." if expect == DRAFT else "Only an Archived version can be rolled back to.")
        current = s.scalar(select(GlossaryVersion).where(GlossaryVersion.glossary_id == v.glossary_id, GlossaryVersion.status == PUBLISHED))
        before = _vview(current, 0) if current else None
        if current is not None:
            current.status = ARCHIVED
        v.status, v.published_by, v.published_at = PUBLISHED, actor, _now()
        s.flush()
        after = _vview(v, 0)
    invalidate()
    return after, before


def publish(version_id: int, actor: str, db: Database | None = None) -> tuple[dict, dict | None]:
    """Make the Draft live; the previous Published version is archived. Returns (now live, previously live)."""
    return _activate(db or get_db(), version_id, actor, DRAFT)


def rollback(version_id: int, actor: str, db: Database | None = None) -> tuple[dict, dict | None]:
    """Put an Archived version back live in one step."""
    return _activate(db or get_db(), version_id, actor, ARCHIVED)


def purge(scope_type: str, scope_id: int, db: Database | None = None) -> None:
    """Its knowledge base or department was deleted: a later one with the same id must not inherit these terms."""
    with (db or get_db()).session() as s:
        g = _glossary(s, scope_type, scope_id)
        if g is None:
            return
        ids = list(s.scalars(select(GlossaryVersion.id).where(GlossaryVersion.glossary_id == g.id)))
        if ids:
            s.execute(delete(GlossaryTerm).where(GlossaryTerm.version_id.in_(ids)))
            s.execute(delete(GlossaryVersion).where(GlossaryVersion.id.in_(ids)))
        s.delete(g)
    invalidate()


# ---- CSV -------------------------------------------------------------------------------------------------------------------
def to_csv(terms: list[Term]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_FIELDS)
    for t in terms:
        w.writerow([t.canonical, "|".join(t.aliases), t.kind, t.language, str(t.case_sensitive).lower(), str(t.enabled).lower(),
                    str(t.stable).lower()])
    return buf.getvalue()


def from_csv(text: str) -> list[dict]:
    """Rows of a CSV with a header line (canonical and aliases are required; aliases are separated by |)."""
    rows = list(csv.DictReader(io.StringIO((text or "").lstrip("﻿"))))
    if rows and not {"canonical", "aliases"} <= set(rows[0]):
        raise ValueError("The CSV needs a header line with at least 'canonical' and 'aliases'.")
    return [{k: v for k, v in r.items() if k in CSV_FIELDS and v is not None} for r in rows]


# ---- resolving ---------------------------------------------------------------------------------------------------------------
def _published(s, scope_type: str, scope_id) -> tuple[list[Term], dict | None]:
    if scope_id is None:
        return [], None
    g = _glossary(s, scope_type, scope_id)
    v = g and s.scalar(select(GlossaryVersion).where(GlossaryVersion.glossary_id == g.id, GlossaryVersion.status == PUBLISHED))
    if not v:
        return [], None
    return [_term(r, scope_type) for r in s.scalars(select(GlossaryTerm).where(GlossaryTerm.version_id == v.id))
            if r.enabled], {"id": v.id, "version": v.version}


def merge(kb_terms: list[Term], dept_terms: list[Term]) -> list[Term]:
    """KB terms win: a department term is left out when any of its forms is already a form of a KB term."""
    taken = {" ".join(words(f)) for t in kb_terms for f in t.forms()}
    return list(kb_terms) + [t for t in dept_terms if not any(" ".join(words(f)) in taken for f in t.forms())]


def terms_for(kb: dict, db: Database | None = None, draft: tuple | None = None) -> tuple[list[Term], dict]:
    """The terms in force for a knowledge base (its own, then its department's) and their versions.
    `draft` = (scope type, scope id, terms) puts a Draft's terms in place of that scope's published ones (the test view)."""
    db = db or get_db()
    key = (id(db), kb.get("id"), kb.get("department_id"))
    if draft is None:
        with _lock:
            hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    try:
        with db.session() as s:
            kb_terms, kb_v = _published(s, KB, kb.get("id"))
            dept_terms, dept_v = _published(s, DEPARTMENT, kb.get("department_id"))
    except Exception:
        log.warning("glossary unavailable; searching without it", exc_info=True)
        return [], {}
    if draft is not None:
        scope_type, scope_id, terms = draft
        terms = [Term(**{**t.__dict__, "source": scope_type}) for t in terms if t.enabled]
        if scope_type == KB and scope_id == kb.get("id"):
            kb_terms, kb_v = terms, {"draft": True}
        elif scope_type == DEPARTMENT and scope_id == kb.get("department_id"):
            dept_terms, dept_v = terms, {"draft": True}
    versions = {k: v for k, v in ((KB, kb_v), (DEPARTMENT, dept_v)) if v}
    out = (merge(kb_terms, dept_terms), versions)
    if draft is None:
        with _lock:
            _cache[key] = (time.monotonic(), out)
    return out


@dataclass
class Expansion:
    search_text: str
    applied: list = field(default_factory=list)       # [{canonical, matched, added, source}]
    versions: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"search_text": self.search_text, "applied": self.applied, "versions": self.versions}


def expand_with(text: str, terms: list[Term], lang: str = "", max_terms: int | None = None, max_aliases: int | None = None,
                versions: dict | None = None) -> Expansion:
    """Append the other forms of every term found in `text`. Longer forms are matched first."""
    if max_terms is None or max_aliases is None:
        g = global_settings.current()
        max_terms = g.glossary_max_terms if max_terms is None else max_terms
        max_aliases = g.glossary_max_aliases if max_aliases is None else max_aliases
    folded, exact = words(text), words(text, True)
    present = set(folded)
    applied, additions = [], []
    candidates = []
    for t in terms:
        if t.language and lang and t.language != lang:
            continue
        hay = exact if t.case_sensitive else folded
        matched = [f for f in t.forms() if _contains(hay, words(f, t.case_sensitive))]
        if matched:
            candidates.append((max(len(words(f)) for f in matched), t, max(matched, key=lambda f: len(words(f)))))
    for _, t, matched in sorted(candidates, key=lambda c: -c[0]):
        if len(applied) >= max_terms:
            break
        if t.kind == "preferred":
            others = [t.canonical] if matched != t.canonical else []
        else:
            others = [f for f in t.forms() if f != matched]
        added = [f for f in others if not set(words(f)) <= present and f not in additions][:max_aliases]
        if added:
            applied.append({"canonical": t.canonical, "matched": matched, "added": added, "source": t.source})
            additions += added
            present |= {w for f in added for w in words(f)}
    search_text = text + (" " + " ".join(additions) if additions else "")
    return Expansion(search_text, applied, versions or {})


def expand(text: str, kb: dict, lang: str = "", stable: bool | None = None, db: Database | None = None) -> Expansion:
    """Expand a search text with a knowledge base's terms. `stable` keeps only stable (True) or only other (False) terms."""
    terms, versions = terms_for(kb, db)
    if stable is not None:
        terms = [t for t in terms if t.stable == stable]
    return expand_with(text, terms, lang, versions=versions)


def mode(kb: dict) -> str:
    return scoped_settings.for_kb(kb, "glossary_mode")


def index_signature(kb: dict, db: Database | None = None) -> str:
    """'<hash> <versions>' of the stable terms a passage of this knowledge base would be indexed with ("" = none)."""
    if mode(kb) != "index":
        return ""
    terms, versions = terms_for(kb, db)
    stable = sorted((t.canonical, tuple(t.aliases), t.kind, t.language, t.case_sensitive) for t in terms if t.stable)
    if not stable:
        return ""
    label = " ".join(f"{k}@v{v['version']}" for k, v in sorted(versions.items()) if "version" in v)
    return (hashlib.sha256(repr(stable).encode()).hexdigest()[:16] + " " + label)[:120]


def index_texts(kb: dict, texts: list[str], langs: list[str], db: Database | None = None) -> list[str] | None:
    """What each passage is embedded as when stable terms apply: the passage plus the other forms it uses. None = as stored."""
    if not index_signature(kb, db):
        return None
    terms, versions = terms_for(kb, db)
    stable = [t for t in terms if t.stable]
    out = [expand_with(t, stable, lang).search_text for t, lang in zip(texts, langs)]
    return None if out == list(texts) else out
