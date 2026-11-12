"""The autonomous admin agent (Release 3, Phase 4; A4, D57, D60, D63): it works the tickets in its queue, with three fixes and a person for the rest.

The loop for one ticket (live mode):
    1. Claim: an atomic change `assigned` -> `in_progress`, so only one runner can win. The event names the actor as autonomous.
    2. Diagnose: read the evidence, the trace, the documents, the knowledge base settings, the glossary and similar past tickets in the same
       knowledge base (read-only tools, bound to THIS ticket). Choose ONE fix, or none.
    3. Apply (the code, not the model): re-ingest one file, add one glossary term, or PROPOSE a setting change. A fix is read before,
       changed, validated after, and rolled back on any error (app/admin_fixes.py).
    4. Complete (`completed`), dismiss (`dismissed`, only when the diagnosis finds no defect and says it is sure), or hand to a person
       (`assigned_to_human`, with the diagnosis in the note) when there is no valid fix, a tool failed, a setting needs approval, a limit
       was reached, or the agent was switched off meanwhile.
In `shadow` mode the agent only diagnoses the tickets people already hold and records what it WOULD do: it claims nothing and changes nothing.

Limits, all in code: a step cap, a token ceiling and a timeout per run (the runtime); one fix per run; `admin_agent_daily_fix_cap` per
knowledge base; the mode (`off` stops it on the next run, and again just before any change); and a sweeper that hands a ticket the agent has
held too long (a crashed run) to a person. What the agent reads is untrusted (evidence can hold instructions): it is data, and a tool call
or a fix is checked against the ticket's own knowledge base, the schema and the allow-lists whatever the model says.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app import admin_fixes, agent_runtime, global_settings, scoped_settings, ticketing, tickets
from app.agent_runtime import Limits, Tool
from app.db import AnswerTrace, Database, Document, Feedback, Ticket, TicketEvent, get_db
from app.knowledge import documents, kbs
from app.tickets import Actor, IllegalTransition

log = logging.getLogger(__name__)
PURPOSE = "fb_admin"
NONE, DISMISS = "none", "dismiss"
CHOICES = admin_fixes.ACTIONS + (NONE, DISMISS)
SYSTEM = ("You are the administrator's assistant for a company knowledge-base assistant. A ticket was raised because a person marked an answer as not helpful. "
          "Find out why with the tools, then choose ONE action. reingest: index one document of this knowledge base again (the file changed, failed or is stale). "
          "glossary: add one term with up to 3 other forms, when the person used different words from the documents. setting: propose changing one knowledge-base "
          "setting (a person must approve it). none: no fix applies, or you are not sure. dismiss: only when the answer was correct and there is no defect, and you are sure. "
          "Prefer none to a guess. Give the diagnosis in at most 3 short sentences, with no personal data.")
FORMAT = ('{"final": {"action": "reingest" | "glossary" | "setting" | "none" | "dismiss", "args": {"doc_id": <number>} | {"canonical": "<term>", "aliases": ["<form>"]} | '
          '{"key": "<setting name>", "value": <value>} | {}, "diagnosis": "<short>", "sure": true | false}}')
_last_run = 0.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d):
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


def limits(db: Database) -> Limits:
    g = global_settings.current(db)
    return Limits(g.admin_agent_max_steps, g.admin_agent_run_seconds, g.admin_agent_token_ceiling)


def mode(db: Database) -> str:
    return global_settings.current(db).admin_agent_mode


def _load(db: Database, ticket_id: int) -> Ticket | None:
    with db.session() as s:
        t = s.get(Ticket, ticket_id)
        if t is not None:
            s.expunge(t)
        return t


# ---- diagnosis tools: read-only, bound to one ticket -------------------------------------------------------------------------------------
def tools_for(db: Database, t: Ticket) -> dict[str, Tool]:
    def cut(v, n=600):
        return (v or "")[:n]

    def get_ticket(_a) -> dict:
        return {"status": t.status, "title": cut(t.title, 200), "evidence": [
            {"question": cut(e.get("question")), "answer": cut(e.get("answer")), "comment": cut(e.get("comment")), "issue": cut(e.get("summary"), 300),
             "replay": {k: cut(str(v), 300) for k, v in (e.get("replay") or {}).items() if k != "replay_answer"}, "documents": e.get("documents") or []}
            for e in (t.evidence or [])[:5]]}

    def first_trace():
        fid = next((e.get("feedback_id") for e in (t.evidence or []) if e.get("feedback_id")), None)
        with db.session() as s:
            fb = s.get(Feedback, fid) if fid else None
            return s.get(AnswerTrace, fb.answer_id) if fb is not None else None

    def get_trace(_a) -> dict:
        tr = first_trace()
        if tr is None:
            return {"error": "The trace is no longer available."}
        scores = [h.get("score") for h in tr.hits or [] if h.get("score") is not None]
        return {"found_in_documents": bool(tr.found), "searched": bool(tr.searched), "search_text": cut(tr.standalone_query, 200), "passages_found": len(tr.hits or []),
                "passages_used": sum(1 for h in tr.hits or [] if h.get("used")), "best_score": round(max(scores), 3) if scores else None,
                "documents_cited": sorted({h["doc_id"] for h in tr.hits or [] if h.get("used") and h.get("doc_id") is not None})}

    def _doc(d: dict) -> dict:
        return {"doc_id": d["id"], "name": d["name"], "status": d["status"], "chunks": d["chunks"], "error": cut(d["error"], 200), "language": d["language"],
                "indexed_at": d["indexed_at"], "updated_at": d["updated_at"], "file_stored": documents.stored(d)}

    def list_documents(_a) -> list:
        with db.session() as s:
            rows = list(s.scalars(select(Document).where(Document.kb_id == t.kb_id).order_by(Document.id).limit(40)))
            return [_doc(documents._view(r)) for r in rows]

    def get_document(a) -> dict:
        d = documents.get(db, a["doc_id"])
        if d is None or d["kb_id"] != t.kb_id:
            return {"error": "That document is not in this ticket's knowledge base."}
        return _doc(d)

    def similar_tickets(_a) -> list:
        with db.session() as s:
            rows = list(s.scalars(select(Ticket).where(Ticket.kb_id == t.kb_id, Ticket.id != t.id, Ticket.status.in_(tickets.CLOSED)).order_by(Ticket.id.desc()).limit(5)))
            out = []
            for r in rows:
                fixes = [(e.detail or {}) for e in s.scalars(select(TicketEvent).where(TicketEvent.ticket_id == r.id, TicketEvent.event == tickets.FIX_APPLIED))]
                out.append({"title": cut(r.title, 120), "outcome": r.status, "fixes": [{"action": f.get("action"), "ok": f.get("ok"), "rolled_back": f.get("rolled_back")} for f in fixes]})
            return out

    def get_settings(_a) -> dict:
        kb = kbs.get_kb(db, t.kb_id) or {}
        return {k: scoped_settings.for_kb(kb, k, db) if kb else None for k in global_settings.current(db).admin_agent_setting_keys}

    def get_glossary(_a) -> dict:
        from app import glossary
        pub = glossary.view("kb", t.kb_id, db)["published"]
        terms = pub["terms"] if pub else []
        return {"terms": len(terms), "list": [{"canonical": x["canonical"], "aliases": x["aliases"]} for x in terms[:30]]}

    return {"get_ticket": Tool("get_ticket", "The ticket's evidence: the question, the answer, the comment, the issue and the replay result.", get_ticket),
            "get_trace": Tool("get_trace", "How the answer was found: whether documents answered, passages found and used, best score, documents cited.", get_trace),
            "list_documents": Tool("list_documents", "The documents of this knowledge base with status, chunks, errors and dates.", list_documents),
            "get_document": Tool("get_document", "One document of this knowledge base.", get_document, {"doc_id": int}),
            "similar_tickets": Tool("similar_tickets", "Recent closed tickets of this knowledge base with the fixes tried and how they ended.", similar_tickets),
            "get_settings": Tool("get_settings", "The knowledge base settings you may propose changing, with their current values.", get_settings),
            "get_glossary": Tool("get_glossary", "The knowledge base glossary: number of terms and the terms.", get_glossary)}


def _validate(db: Database):
    def check(final: dict) -> dict:
        action = final.get("action")
        if action not in CHOICES:
            raise ValueError("action")
        diagnosis = ticketing.mask(db, str(final.get("diagnosis") or "")).strip()[:500]
        if not diagnosis:
            raise ValueError("diagnosis")
        args = final.get("args") if isinstance(final.get("args"), dict) else {}
        shapes = {admin_fixes.REINGEST: {"doc_id"}, admin_fixes.GLOSSARY: {"canonical", "aliases"}, admin_fixes.SETTING: {"key", "value"}, NONE: set(), DISMISS: set()}
        if set(args) != shapes[action]:
            raise ValueError("args")                                 # exactly the arguments of that action, no more
        if action == DISMISS and final.get("sure") is not True:
            action, args = NONE, {}                                  # a dismissal needs the agent to be sure
        return {"action": action, "args": args, "diagnosis": diagnosis}
    return check


def diagnose(db: Database, t: Ticket) -> agent_runtime.RunResult:
    return agent_runtime.run("admin", system=SYSTEM, task=f"Ticket {t.id}: look into it with the tools.", answer_format=FORMAT, validate=_validate(db),
                             tools=tools_for(db, t), limits=limits(db), purpose=PURPOSE, enabled=lambda: mode(db) in ("shadow", "live"))


# ---- one ticket ---------------------------------------------------------------------------------------------------------------------
def _human_target(t: Ticket) -> tuple:
    _, kind, who = ticketing.assignee_for(ticketing.rule_for(t), False)
    return kind, who


def _hand_over(db: Database, t: Ticket, reason: str, diagnosis: str, **detail) -> str:
    try:
        tickets.transition(db, t.id, tickets.ASSIGNED_TO_HUMAN, Actor.agent(), note=diagnosis[:500], expect=tickets.IN_PROGRESS,
                           detail={"reason": reason, "diagnosis": diagnosis[:500], **detail}, assignee=_human_target(t))
    except IllegalTransition:
        return "skipped"
    admin_fixes.audit(admin_fixes.AUTONOMOUS, "ticket.hand_over", f"ticket {t.id}", f"reason={reason}")
    ticketing._alert("assigned", t.id)
    return "handed_over:" + reason


def fixes_today(db: Database, kb_id: int) -> int:
    start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    with db.session() as s:
        return s.scalar(select(func.count()).select_from(TicketEvent).join(Ticket, Ticket.id == TicketEvent.ticket_id).where(
            Ticket.kb_id == kb_id, TicketEvent.event == tickets.FIX_APPLIED, TicketEvent.created_at >= start)) or 0


def handle(db: Database | None, ticket_id: int, *, shadow: bool = False) -> str:
    """Work one ticket. -> skipped | shadow | completed | dismissed | handed_over:<reason>."""
    db = db or get_db()
    t = _load(db, ticket_id)
    if t is None or mode(db) != ("shadow" if shadow else "live"):
        return "skipped"
    if shadow:
        if t.status in tickets.CLOSED or t.status == tickets.NEW:
            return "skipped"
    else:
        if t.status != tickets.ASSIGNED or t.assignee_type != tickets.AUTONOMOUS:
            return "skipped"
        try:                                                         # 1. the claim: only one runner can win
            tickets.transition(db, ticket_id, tickets.IN_PROGRESS, Actor.agent(), expect=tickets.ASSIGNED, note="Claimed by the admin agent")
        except IllegalTransition:
            return "skipped"
        admin_fixes.audit(admin_fixes.AUTONOMOUS, "ticket.claim", f"ticket {ticket_id}")
    try:
        res = diagnose(db, t)                                        # 2. diagnose
    except Exception:
        res = agent_runtime.RunResult(agent_runtime.ERROR)
    if not res.ok:
        if shadow:
            return "skipped"
        return _hand_over(db, t, res.status, "The agent could not finish its diagnosis, so a person should look at this ticket.")
    out = res.output
    tickets.record(db, ticket_id, tickets.DIAGNOSIS, Actor.agent(), out["diagnosis"], {"action": out["action"], "args": out["args"], "shadow": shadow,
                                                                                         "diagnosis": out["diagnosis"], "tools": res.calls})
    if shadow:                                                       # recorded, and nothing changed anywhere
        return "shadow"
    action, diagnosis = out["action"], out["diagnosis"]
    if action == NONE:
        return _hand_over(db, t, "no_valid_fix", diagnosis)
    if action == DISMISS:
        try:
            tickets.transition(db, ticket_id, tickets.DISMISSED, Actor.agent(), expect=tickets.IN_PROGRESS, note=diagnosis,
                               detail={"diagnosis": diagnosis, "action": DISMISS, "before": None, "after": None, "rolled_back": False})
        except IllegalTransition:
            return "skipped"
        admin_fixes.audit(admin_fixes.AUTONOMOUS, "ticket.dismiss", f"ticket {ticket_id}")
        return "dismissed"
    if action == admin_fixes.SETTING:                                # 3. a setting is only ever a proposal
        try:
            proposal = admin_fixes.propose_setting(db, t, out["args"]["key"], out["args"]["value"])
        except admin_fixes.FixError as e:
            return _hand_over(db, t, e.code, diagnosis, action=action)
        tickets.record(db, ticket_id, tickets.PROPOSAL, Actor.agent(), diagnosis, {**proposal, "diagnosis": diagnosis})
        admin_fixes.audit(admin_fixes.AUTONOMOUS, "ticket.propose_setting", f"ticket {ticket_id}", f"key={proposal['key']}")
        result = _hand_over(db, t, "approval_needed", diagnosis, action=action, key=proposal["key"])
        from app import ticket_alerts
        ticket_alerts.notify(db, "approval", ticket_id)
        return result
    cap = global_settings.current(db).admin_agent_daily_fix_cap       # a file or a glossary term
    if mode(db) != "live":
        return _hand_over(db, t, "agent_off", diagnosis, action=action)
    if fixes_today(db, t.kb_id) >= cap:
        return _hand_over(db, t, "daily_cap", diagnosis, action=action)
    done = admin_fixes.apply(db, ticket_id, action, out["args"], Actor.agent(), admin_fixes.AUTONOMOUS, diagnosis)
    detail = {"diagnosis": diagnosis, "action": action, "before": done["before"], "after": done["after"], "rolled_back": done["rolled_back"]}
    if not done["ok"]:
        return _hand_over(db, t, done["error"] or "fix_failed", diagnosis, **{k: v for k, v in detail.items() if k != "diagnosis"})
    try:
        tickets.transition(db, ticket_id, tickets.COMPLETED, Actor.agent(), expect=tickets.IN_PROGRESS, note=diagnosis, detail=detail)
    except IllegalTransition:
        return "skipped"
    admin_fixes.audit(admin_fixes.AUTONOMOUS, "ticket.complete", f"ticket {ticket_id}", f"action={action}")
    return "completed"


# ---- the loop -------------------------------------------------------------------------------------------------------------------------
def sweep(db: Database | None = None) -> int:
    """Tickets the agent has held in progress for longer than `admin_agent_timeout_minutes` (a crashed or hung run) go to a person."""
    db = db or get_db()
    cutoff = _now() - timedelta(minutes=global_settings.current(db).admin_agent_timeout_minutes)
    with db.session() as s:
        ids = list(s.scalars(select(Ticket.id).where(Ticket.status == tickets.IN_PROGRESS, Ticket.assignee_type == tickets.AUTONOMOUS, Ticket.updated_at < cutoff)))
    n = 0
    for tid in ids:
        t = _load(db, tid)
        try:
            tickets.transition(db, tid, tickets.ASSIGNED_TO_HUMAN, Actor.system(), expect=tickets.IN_PROGRESS, note="The agent did not finish in time, so a person should look at this ticket.",
                               detail={"reason": "timeout", "diagnosis": "The agent's run did not finish."}, assignee=_human_target(t))
        except IllegalTransition:
            continue
        admin_fixes.audit("system (admin agent sweeper)", "ticket.sweep", f"ticket {tid}", "reason=timeout")
        ticketing._alert("assigned", tid)
        n += 1
    return n


def run_once(db: Database | None = None, limit: int = 5) -> dict:
    db = db or get_db()
    out: dict = {"swept": sweep(db)}
    m = mode(db)
    if m == "off":
        return out
    with db.session() as s:
        if m == "live":
            ids = list(s.scalars(select(Ticket.id).where(Ticket.status == tickets.ASSIGNED, Ticket.assignee_type == tickets.AUTONOMOUS).order_by(Ticket.id).limit(limit)))
        else:                                                        # shadow: the tickets people hold, once each
            seen = select(TicketEvent.ticket_id).where(TicketEvent.event == tickets.DIAGNOSIS)
            ids = list(s.scalars(select(Ticket.id).where(Ticket.status == tickets.ASSIGNED_TO_HUMAN, Ticket.id.not_in(seen)).order_by(Ticket.id).limit(limit)))
    for tid in ids:
        r = handle(db, tid, shadow=(m == "shadow"))
        key = r.split(":")[0]
        out[key] = out.get(key, 0) + 1
    return out


def job() -> None:
    """Every minute; it works when `admin_agent_interval_minutes` have passed since its last run (a setting, so no restart is needed)."""
    global _last_run
    try:
        db = get_db()
        if time.monotonic() - _last_run < global_settings.current(db).admin_agent_interval_minutes * 60 - 1 and _last_run:
            sweep(db)
            return
        _last_run = time.monotonic()
        run_once(db)
    except Exception:
        log.warning("the admin agent job failed", exc_info=True)


def register() -> None:
    from app.jobs import scheduler
    scheduler.add_job(job, "interval", "admin-agent", lock_ttl_seconds=900, seconds=60)
