"""Who sees real personal data (12d), and the PII protection switch (12g).

Protection (on by default) is a master switch on the Guardrails screen; each knowledge base inherits it or overrides it
(on / off). Changing either needs a reason and is audited.

* Protection ON for a knowledge base: its documents' values stay behind tokens. People on its *Can see personal data*
  list (groups and people, empty by default) see the real values in answers and source snippets; everyone else sees
  labels such as <EMAIL_ADDRESS>. Every reveal is audited (`pii.reveal`, a count, never the values).
* Protection OFF: everyone sees the real values; passages are revealed before they reach the AI model and the search
  filter is skipped. Question masking and answer redaction stop only when EVERY knowledge base a channel can search is
  off, because a question is masked before anyone knows which knowledge base will answer it.

Indexing tokenises whatever the switch says, so the search index never holds raw values and the switch applies within
seconds, with no re-index. Stored answers keep tokens (values are put back for display only), except what the person
typed or the model wrote while protection was off.
"""
import logging

from sqlalchemy import delete, select

from app.db import Database, Group, KBPiiViewer, KnowledgeBase, User, get_db

log = logging.getLogger(__name__)
MODES = ("inherit", "on", "off")


# ---- the switch ----------------------------------------------------------------------------------------------------
def master(db: Database | None = None) -> bool:
    from app import guardrails
    return guardrails.current(db or get_db()).pii_protection


def protected(db: Database, kb: dict | int) -> bool:
    """Whether a knowledge base's personal data is protected right now."""
    mode = kb.get("pii_protection", "inherit") if isinstance(kb, dict) else _mode(db, kb)
    if mode == "on":
        return True
    if mode == "off":
        return False
    return master(db)


def _mode(db: Database, kb_id: int) -> str:
    with db.session() as s:
        kb = s.get(KnowledgeBase, kb_id)
        return (kb.pii_protection or "inherit") if kb else "on"          # unknown: protect


def all_off(db: Database, kbs: list[dict]) -> bool:
    """True only when every knowledge base (a channel's set) is unprotected; then questions and answers are not masked."""
    return bool(kbs) and not any(protected(db, kb) for kb in kbs)


def _check_reason(reason: str) -> str:
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise ValueError("Give a reason (at least 10 characters). It is recorded in the audit log.")
    return reason[:300]


def set_master(db: Database, enabled: bool, reason: str, actor: str) -> bool:
    from app import guardrails
    from app.db import GuardrailSettings
    reason = _check_reason(reason)
    guardrails.current(db)                                  # make sure the row exists
    with db.session() as s:
        row = s.get(GuardrailSettings, 1)
        before, row.pii_protection, row.updated_by = row.pii_protection, bool(enabled), actor
    guardrails.invalidate()
    _audit(actor, "pii.protection", "all knowledge bases", f"{'on' if before else 'off'} -> {'on' if enabled else 'off'}; reason: {reason}")
    refresh_alert(db)
    return bool(enabled)


def set_kb_mode(db: Database, kb_id: int, mode: str, reason: str, actor: str) -> str:
    if mode not in MODES:
        raise ValueError("Choose inherit, on or off.")
    reason = _check_reason(reason)
    with db.session() as s:
        kb = s.get(KnowledgeBase, kb_id)
        if kb is None:
            raise ValueError("No such knowledge base.")
        before, kb.pii_protection, name = kb.pii_protection or "inherit", mode, kb.name
    _audit(actor, "pii.protection", name, f"{before} -> {mode}; reason: {reason}")
    refresh_alert(db)
    return mode


def unprotected(db: Database) -> list[str]:
    """Names of the knowledge bases whose personal data is not protected right now."""
    from app.knowledge import kbs
    return [kb["name"] for kb in kbs.list_kbs(db) if not protected(db, kb)]


def refresh_alert(db: Database) -> None:
    """A *Needs your attention* item stays open while any knowledge base is unprotected."""
    from app import alerts
    try:
        names = unprotected(db)
        if names:
            alerts.raise_alert(db, "pii_protection_off", "pii_protection_off",
                               "Personal data is not protected in: " + ", ".join(names) + ". Everyone sees the real values, "
                               "and the AI model receives them. Turn PII protection back on under Guardrails or in KB configuration.",
                               "warning")
        else:
            alerts.resolve_key(db, "pii_protection_off")
    except Exception:
        log.warning("could not update the PII protection alert", exc_info=True)


# ---- who may see values --------------------------------------------------------------------------------------------
def viewers(db: Database, kb_id: int) -> dict:
    with db.session() as s:
        rows = list(s.execute(select(KBPiiViewer.kind, KBPiiViewer.ref_id).where(KBPiiViewer.kb_id == kb_id)).all())
    return {"group_ids": sorted(r for k, r in rows if k == "group"), "user_ids": sorted(r for k, r in rows if k == "user")}


def set_viewers(db: Database, kb_id: int, group_ids, user_ids) -> dict:
    gids, uids = sorted({int(i) for i in group_ids or ()}), sorted({int(i) for i in user_ids or ()})
    with db.session() as s:
        if gids and set(s.scalars(select(Group.id).where(Group.id.in_(gids)))) != set(gids):
            raise ValueError("One of the chosen groups does not exist.")
        if uids and set(s.scalars(select(User.id).where(User.id.in_(uids)))) != set(uids):
            raise ValueError("One of the chosen people does not exist.")
        s.execute(delete(KBPiiViewer).where(KBPiiViewer.kb_id == kb_id))
        s.add_all([KBPiiViewer(kb_id=kb_id, kind="group", ref_id=i) for i in gids] +
                  [KBPiiViewer(kb_id=kb_id, kind="user", ref_id=i) for i in uids])
    return {"group_ids": gids, "user_ids": uids}


def describe(db: Database, kb_id: int) -> str:
    v = viewers(db, kb_id)
    with db.session() as s:
        groups = sorted(s.scalars(select(Group.name).where(Group.id.in_(v["group_ids"])))) if v["group_ids"] else []
        people = sorted(s.scalars(select(User.email).where(User.id.in_(v["user_ids"])))) if v["user_ids"] else []
    return ", ".join(groups + people) or "nobody"


def can_see(db: Database, kb_id: int, who) -> bool:
    """On the knowledge base's list, directly or through a group (read NOW; nobody is on it by default)."""
    if who is None or not who.active:
        return False
    v = viewers(db, kb_id)
    return (who.user_id is not None and who.user_id in v["user_ids"]) or bool(set(v["group_ids"]) & set(who.group_ids))


# ---- showing text ---------------------------------------------------------------------------------------------------
def for_viewer(text: str, kb_ids, who, db: Database | None = None, where: str = "answer") -> str:
    """Text as `who` may see it: real values for the knowledge bases that are unprotected or that `who` may see,
    plain labels for everything else. Reveals under protection are audited."""
    from app import pii
    if not text or not pii.TOKEN_RE.search(text):
        return text
    db = db or get_db()
    open_kbs, listed = [], []
    for kb_id in dict.fromkeys(kb_ids or ()):
        if not protected(db, kb_id):
            open_kbs.append(kb_id)
        elif can_see(db, kb_id, who):
            listed.append(kb_id)
    if listed:
        text, n = pii.reveal(text, listed, db)
        if n:
            _audit(getattr(who, "email", "") or "unknown", "pii.reveal", where, f"{n} value(s); knowledge bases {listed}")
    if open_kbs:
        text, _ = pii.reveal(text, open_kbs, db)
    return pii.label(text)


def open_passages(chunks: list[dict], db: Database | None = None) -> list[dict]:
    """Passages of unprotected knowledge bases with their values put back, before the AI model sees them."""
    from app import pii
    db = db or get_db()
    memo: dict = {}
    for c in chunks:
        kb_id = c.get("kb_id")
        if kb_id is None:
            continue
        if kb_id not in memo:
            memo[kb_id] = protected(db, kb_id)
        if not memo[kb_id]:
            c["text"], _ = pii.reveal(c["text"], [kb_id], db)
    return chunks


def _audit(actor: str, action: str, target: str, detail: str) -> None:
    try:
        from app.channels import get_store
        store = get_store()
        if store:
            store.record(actor, action, target[:254], detail)
    except Exception:
        log.warning("could not audit %s", action, exc_info=True)
