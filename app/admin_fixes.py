"""The admin agent's fix layer (Release 3, Phase 4; D57): three fixes, and nothing else, each through the same code a person's click uses.

* `reingest`: index ONE file of the ticket's own knowledge base again. Validated (status Indexed, chunks > 0, the first chunk can be read).
  The index step keeps the old chunks until the new ones are in and puts them back on failure, so a failed re-index leaves the file serving.
* `glossary`: add one term to the ticket's own knowledge base glossary as a new version. Validated before it goes live (the conflicts report
  and an expansion test) and again after; rollback is publishing the previous version again.
* `setting`: a PROPOSAL only (key, scope, before, proposed), recorded on the ticket. A person approves it, and then the same
  apply-and-validate routine runs with the approver as the actor.

`apply()` is the one routine for the agent and for an approver. Before: read the current value. Modify. After: validate. On any error: put
the before value back and say so (`rolled_back`). Every attempt writes a `fix_applied` event (diagnosis, action, before, after,
rolled_back) and an audit entry with the actor; the audit holds codes and numbers, never free text.

Each tool checks, in code, that the document or knowledge base is the TICKET's own, that the arguments match the schema, and that the
action and the setting key are on the allow-list. What the model says is never trusted for any of these.
"""
import logging

from app import glossary, global_settings, scoped_settings, tickets
from app.db import Database, Ticket, TicketEvent, get_db
from app.knowledge import documents, kbs
from app.tickets import Actor, IllegalTransition, NotAllowed, NotFound, TicketError
from sqlalchemy import select

log = logging.getLogger(__name__)
AUTONOMOUS = "autonomous (admin agent)"
REINGEST, GLOSSARY, SETTING = "reingest", "glossary", "setting"
ACTIONS = (REINGEST, GLOSSARY, SETTING)
MAX_ALIASES, MAX_FORM_CHARS = 3, 60


class FixError(Exception):
    """A refusal or a failed check. `code` is a short stable word: it is what gets stored, never free text."""
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def audit(label: str, action: str, target: str, detail: str = "") -> None:
    try:
        from app.channels import get_store
        store = get_store()
        if store:
            store.record(label, action, target, detail)
    except Exception:
        log.warning("could not audit a ticket fix", exc_info=True)


def _ticket(db: Database, ticket_id: int) -> Ticket:
    with db.session() as s:
        t = s.get(Ticket, ticket_id)
        if t is None:
            raise NotFound("No such ticket.")
        s.expunge(t)
        return t


def _kb(db: Database, t: Ticket) -> dict:
    kb = kbs.get_kb(db, t.kb_id) if t.kb_id else None
    if kb is None:
        raise FixError("no_knowledge_base")
    return kb


# ---- 1. re-ingest one file -----------------------------------------------------------------------------------------------------------
def _doc_state(d: dict) -> dict:
    return {"status": d["status"], "chunks": d["chunks"], "error": d["error"], "indexed_at": d["indexed_at"].isoformat() if d["indexed_at"] else None,
            "content_hash": d["content_hash"]}


def reingest(db: Database, t: Ticket, doc_id) -> dict:
    kb = _kb(db, t)
    if not isinstance(doc_id, int) or isinstance(doc_id, bool):
        raise FixError("bad_arguments")
    d = documents.get(db, doc_id)
    if d is None or d["kb_id"] != kb["id"]:
        raise FixError("outside_knowledge_base")                    # a document of another knowledge base, or none at all
    if not documents.stored(d):
        raise FixError("file_missing")
    try:
        documents._not_rebuilding(db, kb["id"])
    except ValueError:
        raise FixError("kb_rebuilding")
    before = _doc_state(d)
    documents._set(db, doc_id, status="Pending", error="")
    documents.index_document(db, doc_id)                            # never raises: a problem becomes status Failed, and the old chunks are put back
    after_doc = documents.get(db, doc_id)
    after = _doc_state(after_doc)
    valid = after_doc["status"] == "Indexed" and after_doc["chunks"] > 0 and bool(kbs.store_for(kb).get([f"{after_doc['doc_key']}::0"]))
    if valid:
        return {"before": before, "after": after, "valid": True, "rolled_back": False}
    documents._set(db, doc_id, status=before["status"], error=before["error"], chunks=before["chunks"])       # the file is back as it was
    return {"before": before, "after": _doc_state(documents.get(db, doc_id)), "valid": False, "rolled_back": True, "error": "validation_failed"}


# ---- 2. one glossary term ------------------------------------------------------------------------------------------------------------
def _clean_term(canonical, aliases) -> tuple[str, list]:
    if not isinstance(canonical, str) or not isinstance(aliases, list):
        raise FixError("bad_arguments")
    canonical = " ".join(canonical.split())
    aliases = list(dict.fromkeys(" ".join(a.split()) for a in aliases if isinstance(a, str) and a.strip()))
    if not canonical or not aliases or len(aliases) > MAX_ALIASES or len(canonical) > MAX_FORM_CHARS or any(len(a) > MAX_FORM_CHARS for a in aliases):
        raise FixError("bad_arguments")
    return canonical, aliases


def _key(text: str) -> str:
    return " ".join(glossary.words(text))


def glossary_term(db: Database, t: Ticket, canonical, aliases, label: str) -> dict:
    kb = _kb(db, t)
    canonical, aliases = _clean_term(canonical, aliases)
    if scoped_settings.for_kb(kb, "glossary_mode", db) == "off":
        raise FixError("glossary_mode_off")                         # a term would do nothing: turn the mode on first (a setting proposal)
    published = glossary.view("kb", kb["id"], db)["published"]
    existing = [{k: v for k, v in x.items() if k != "id"} for x in (published["terms"] if published else [])]
    if any(_key(x["canonical"]) == _key(canonical) for x in existing):
        raise FixError("term_exists")
    new = {"canonical": canonical, "aliases": aliases, "kind": "synonym"}
    try:
        terms = glossary.clean_terms(existing + [new])
    except ValueError:
        raise FixError("invalid_term")
    forms = {_key(f) for f in [canonical] + aliases}
    if [c for c in glossary.conflicts(terms) if _key(c["form"]) in forms]:
        raise FixError("conflict")
    sample = f"what about {aliases[0]}"
    if not any(a["canonical"] == canonical for a in glossary.expand_with(sample, terms, versions={}).applied):
        raise FixError("expansion_test_failed")
    before = {"published_version": published["id"] if published else None, "terms": len(existing)}
    glossary.save_draft("kb", kb["id"], existing + [new], label, db)
    draft = glossary.view("kb", kb["id"], db)["draft"]
    live, previous = glossary.publish(draft["id"], label, db)
    try:
        ok = any(a["canonical"] == canonical for a in glossary.expand(sample, kb, db=db).applied)
        count = len(glossary.view("kb", kb["id"], db)["published"]["terms"])
        if not ok or count != before["terms"] + 1:
            raise FixError("validation_failed")
    except Exception as e:
        _unpublish(db, live, previous, label)
        return {"before": before, "after": {"published_version": previous["id"] if previous else None, "terms": before["terms"]}, "valid": False,
                "rolled_back": True, "error": e.code if isinstance(e, FixError) else "validation_failed"}
    return {"before": before, "after": {"published_version": live["id"], "terms": count}, "valid": True, "rolled_back": False}


def _unpublish(db: Database, live: dict, previous: dict | None, label: str) -> None:
    """Put the glossary back: publish the previous version again, or, if there was none, retire the new one."""
    try:
        if previous is not None:
            glossary.rollback(previous["id"], label, db)
        else:
            glossary.retire(live["id"], db)
    except Exception:
        log.warning("could not roll the glossary back", exc_info=True)


# ---- 3. a setting: only ever a proposal until a person approves ----------------------------------------------------------------------------
def _approved_key(db: Database, key) -> str:
    if not isinstance(key, str) or key not in global_settings.current(db).admin_agent_setting_keys:
        raise FixError("key_not_allowed")                           # not on the Admin-approved list
    reg = scoped_settings.REGISTRY.get(key)
    if reg is None or scoped_settings.KB not in reg.scopes:
        raise FixError("key_not_allowed")                           # or not a setting a knowledge base may override
    return key


def _override(db: Database, kb_id: int, key: str):
    from app.db import ScopedSetting
    with db.session() as s:
        row = s.scalar(select(ScopedSetting).where(ScopedSetting.scope_type == scoped_settings.KB, ScopedSetting.scope_id == str(kb_id), ScopedSetting.key == key))
        return (True, row.value) if row is not None else (False, None)


def propose_setting(db: Database, t: Ticket, key, value) -> dict:
    """Validate a proposed change and describe it. Changes nothing."""
    kb = _kb(db, t)
    key = _approved_key(db, key)
    try:
        proposed = scoped_settings._clean(key, value, db)
    except ValueError:
        raise FixError("invalid_value")
    had, was = _override(db, kb["id"], key)
    return {"key": key, "scope": scoped_settings.KB, "scope_id": kb["id"], "before": scoped_settings.for_kb(kb, key, db), "proposed": proposed,
            "had_override": had, "override_before": was}


def _json(v):
    return v if isinstance(v, (str, int, float, bool, type(None))) else list(v) if isinstance(v, (list, tuple)) else str(v)


def _apply_setting(db: Database, t: Ticket, proposal: dict, label: str) -> dict:
    kb = _kb(db, t)
    key = _approved_key(db, proposal.get("key"))                    # re-checked: the list may have changed since it was proposed
    if proposal.get("scope") != scoped_settings.KB or proposal.get("scope_id") != kb["id"]:
        raise FixError("outside_knowledge_base")
    had, was = _override(db, kb["id"], key)
    before = _json(scoped_settings.for_kb(kb, key, db))
    try:
        scoped_settings.put(scoped_settings.KB, kb["id"], {key: proposal["proposed"]}, label, db)
        after = _json(scoped_settings.for_kb(kb, key, db))
        if _json(scoped_settings._clean(key, proposal["proposed"], db)) != after:
            raise FixError("validation_failed")
    except Exception as e:
        if had:
            scoped_settings.put(scoped_settings.KB, kb["id"], {key: was}, label, db)
        else:
            scoped_settings.reset(scoped_settings.KB, kb["id"], key, db)
        return {"before": before, "after": _json(scoped_settings.for_kb(kb, key, db)), "valid": False, "rolled_back": True,
                "error": e.code if isinstance(e, FixError) else "validation_failed"}
    return {"before": before, "after": after, "valid": True, "rolled_back": False}


# ---- the routine for the agent and for an approver --------------------------------------------------------------------------------------
def apply(db: Database | None, ticket_id: int, action: str, args: dict, actor: Actor, label: str, diagnosis: str = "") -> dict:
    """Run one fix and record it. -> {ok, action, before, after, rolled_back, error}. Never raises for a refused or failed fix: the caller hands
    the ticket to a person on `ok == False`. `setting` is applied here only with an approved proposal (see `approve`)."""
    db = db or get_db()
    t = _ticket(db, ticket_id)
    out: dict = {"ok": False, "action": action, "before": None, "after": None, "rolled_back": False, "error": ""}
    try:
        if action not in ACTIONS or (action == SETTING and actor.kind == "agent"):
            raise FixError("action_not_allowed")                    # the agent never applies a setting; only a person does, through `approve`
        if t.status in tickets.CLOSED:
            raise FixError("ticket_closed")
        if action == REINGEST:
            r = reingest(db, t, args.get("doc_id"))
        elif action == GLOSSARY:
            r = glossary_term(db, t, args.get("canonical"), args.get("aliases"), label)
        else:
            r = _apply_setting(db, t, args, label)
        out.update(ok=r["valid"], before=r["before"], after=r["after"], rolled_back=r["rolled_back"], error=r.get("error", ""))
    except FixError as e:
        out["error"] = e.code
    except Exception as e:                                          # a fix that crashes is reported, never left half done
        log.warning("ticket %s: the %s fix failed (%s)", ticket_id, action, type(e).__name__)
        out["error"] = "fix_failed"
    tickets.record(db, ticket_id, tickets.FIX_APPLIED, actor, "", {"action": action, "ok": out["ok"], "before": out["before"], "after": out["after"],
                                                                  "rolled_back": out["rolled_back"], "error": out["error"], "diagnosis": diagnosis[:500]})
    audit(label, f"ticket_fix.{action}", f"ticket {ticket_id}", f"{'ok' if out['ok'] else 'failed'}; rolled_back={out['rolled_back']}; error={out['error'] or '-'}")
    return out


def proposal_event(db: Database, ticket_id: int) -> dict | None:
    """The latest proposal on the ticket that no one has decided yet: {event_id, key, scope, scope_id, before, proposed, diagnosis}."""
    with db.session() as s:
        rows = list(s.scalars(select(TicketEvent).where(TicketEvent.ticket_id == ticket_id,
                                                        TicketEvent.event.in_((tickets.PROPOSAL, tickets.PROPOSAL_DECIDED))).order_by(TicketEvent.id)))
    decided = {(r.detail or {}).get("proposal_id") for r in rows if r.event == tickets.PROPOSAL_DECIDED}
    open_ones = [r for r in rows if r.event == tickets.PROPOSAL and r.id not in decided]
    if not open_ones:
        return None
    r = open_ones[-1]
    return {"event_id": r.id, **(r.detail or {})}


def approve(db: Database | None, ticket_id: int, actor: Actor, label: str, *, approve: bool = True, note: str = "") -> dict:
    """A person approves or declines the open setting proposal. Approving runs the apply-and-validate routine with the person as the actor; the
    ticket then stays with them to complete. Only an Admin, or an Editor of the ticket's department, may decide."""
    db = db or get_db()
    t = _ticket(db, ticket_id)
    if not tickets.is_manager(actor, t):
        raise NotAllowed("Only an Admin, or an Editor of the department, can decide on a proposal.")
    proposal = proposal_event(db, ticket_id)
    if proposal is None:
        raise IllegalTransition("There is no proposal waiting for a decision.")
    out = {"decision": "approved" if approve else "declined", "ok": True, "rolled_back": False, "error": ""}
    if approve:
        try:
            r = _apply_setting(db, t, proposal, label)
            out.update(ok=r["valid"], rolled_back=r["rolled_back"], error=r.get("error", ""), before=r["before"], after=r["after"])
        except FixError as e:
            out.update(ok=False, error=e.code)
    tickets.record(db, ticket_id, tickets.PROPOSAL_DECIDED, actor, (note or "").strip()[:500],
                   {"proposal_id": proposal["event_id"], "decision": out["decision"], "key": proposal.get("key"), "ok": out["ok"],
                    "before": out.get("before"), "after": out.get("after"), "rolled_back": out["rolled_back"], "error": out["error"]})
    audit(label, f"ticket_fix.setting.{out['decision']}", f"ticket {ticket_id}", f"key={proposal.get('key')}; ok={out['ok']}; rolled_back={out['rolled_back']}")
    if actor.user_id is not None and t.status == tickets.ASSIGNED_TO_HUMAN and not (t.assignee_type == tickets.USER and t.assignee_id == actor.user_id):
        tickets.reassign(db, ticket_id, tickets.USER, actor.user_id, actor, "Approved a proposal" if approve else "Declined a proposal")
    return out
