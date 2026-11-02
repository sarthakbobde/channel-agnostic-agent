"""Admin API for answer traces (D23). Admins see any trace; an Editor sees the conversations and traces of their own departments.

Passage text is fetched only when a trace is opened and only for documents the viewer may see, worked out from the database
now (knowledge base permission, document visibility, then the access stored on the chunk). Anything else shows as restricted,
without its document, passage id or text. An Editor's conversation view leaves out each answer's stored sources, because
their snippets are passage text from documents the Editor may not be allowed to see. Every view is audited, without text.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from app import identity, memory
from app.admin.api import AdminContext, Session, admin_session, get_ctx
from app.admin.conversations_api import conversation_list
from app.db import AnswerTrace, Conversation, Prompt, UsageRecord
from app.knowledge import access, kbs

router = APIRouter(prefix="/admin/api", tags=["admin"])
log = logging.getLogger(__name__)


def _departments(who) -> set[int] | None:
    """The departments whose conversations this person may open; None means all of them."""
    return None if who.role == "admin" else set(who.edits_departments)


def _open(ctx: AdminContext, conv_id: str, who) -> dict:
    """The conversation's details, or 404 (the same answer whether it is missing or belongs to another department)."""
    mine = _departments(who)
    with ctx.db.session() as s:
        c = s.get(Conversation, conv_id)
        if c is None or (mine is not None and c.department_id not in mine):
            raise HTTPException(404, "No such conversation.")
        return {"id": c.id, "channel": c.channel, "person": c.owner_email or "unknown", "department": c.department, "title": c.title}


@router.get("/traces/conversations")
def list_conversations(department: str = "", channel: str = "", person: str = "", date_from: str = "", date_to: str = "", limit: int = 50,
                       offset: int = 0, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    mine = _departments(identity.resolve(sess.email, ctx.db))
    return conversation_list(ctx, department, channel, person, date_from, date_to, limit, offset, only_departments=mine)


@router.get("/traces/conversations/{conv_id}")
def open_conversation(conv_id: str, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    info = _open(ctx, conv_id, identity.resolve(sess.email, ctx.db))
    ctx.store.record(sess.email, "conversation.view", conv_id)
    return {**info, "messages": [{k: v for k, v in m.items() if k != "meta"} for m in memory.get_messages(conv_id, ctx.db)]}


RESTRICTED = {"status": "restricted", "title": "", "doc_id": None, "chunk_id": None, "text": ""}


def _passages(ctx: AdminContext, hits: list[dict], who) -> list[dict]:
    """Each hit with its passage, or marked restricted or unavailable, for this viewer."""
    out = []
    by_kb: dict = {}
    for h in hits:
        by_kb.setdefault(h["kb_id"], []).append(h)
    fetched: dict = {}
    for kb_id, group in by_kb.items():
        kb = kbs.get_kb(ctx.db, kb_id) if kb_id else None
        if kb is None or not access.can_query(ctx.db, kb, who):
            fetched[kb_id] = (kb, {}, {})
            continue
        docs = access.visible_docs(ctx.db, kb, who)
        wanted = [h["chunk_id"] for h in group if h["chunk_id"] and h["doc_id"] in docs.values()]
        try:
            rows = {r["id"]: r for r in kbs.store_for(kb).get(wanted)}
        except Exception as e:
            log.warning("could not fetch passages for a trace (%s)", type(e).__name__)
            rows = {}
        fetched[kb_id] = (kb, docs, rows)
    for h in hits:
        kb, docs, rows = fetched[h["kb_id"]]
        base = {k: h[k] for k in ("kb_id", "score", "rank", "via", "above_threshold", "used")}
        row = rows.get(h["chunk_id"])
        if h["doc_id"] not in docs.values():
            gone = kb is None or (access.manages(who, kb) and h["doc_id"] is not None)   # it may see everything, so the document is gone
            out.append({**base, **RESTRICTED, "status": "unavailable" if gone else "restricted"})
        elif row is None:
            out.append({**base, **RESTRICTED, "status": "unavailable", "doc_id": h["doc_id"], "chunk_id": h["chunk_id"]})
        elif row["metadata"].get("doc_id") not in docs or not access.permits(row["metadata"], who, kb):
            out.append({**base, **RESTRICTED})                                           # the chunk's own access has the last word
        else:
            out.append({**base, "status": "visible", "doc_id": h["doc_id"], "chunk_id": h["chunk_id"],
                        "title": row["metadata"].get("source") or "", "text": row["text"]})
    return out


@router.get("/traces/{answer_id}")
def read_trace(answer_id: int, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    who = identity.resolve(sess.email, ctx.db)
    with ctx.db.session() as s:
        t = s.get(AnswerTrace, answer_id)
        if t is None:
            raise HTTPException(404, "No trace was recorded for this answer.")
        _open(ctx, t.conversation_id, who)                 # 404 for someone else's department
        ids = {v["id"] for v in (t.prompt_versions or {}).values()}
        prompts = {p.id: p for p in s.scalars(select(Prompt).where(Prompt.id.in_(ids)))} if ids else {}
        usage = [{"ts": u.ts, "provider": u.provider, "model": u.model, "purpose": u.purpose, "input_tokens": u.input_tokens,
                  "output_tokens": u.output_tokens, "cost": u.cost, "status": u.status}
                 for u in s.scalars(select(UsageRecord).where(UsageRecord.message_id == answer_id).order_by(UsageRecord.id))]
        view = {"answer_id": t.answer_id, "question_id": t.question_id, "conversation_id": t.conversation_id, "complete": t.complete,
                "found": t.found, "searched": t.searched, "standalone_query": t.standalone_query, "search_text": t.search_text,
                "retrieval": t.retrieval, "provider": t.provider, "model": t.model, "fallback_used": t.fallback_used, "timings": t.timings,
                "language": t.language, "language_confidence": t.language_confidence, "glossary": t.glossary,
                "config_snapshot_id": t.config_snapshot_id, "created_at": t.created_at, "usage": usage,
                "prompts": [{"scope": scope, "id": v["id"], "version": v["version"], "status": prompts[v["id"]].status if v["id"] in prompts else "deleted"}
                            for scope, v in (t.prompt_versions or {}).items()]}
        hits = list(t.hits or [])
    view["hits"] = _passages(ctx, hits, who)
    names = {k["id"]: k["name"] for k in kbs.list_kbs(ctx.db)}
    for row in view["hits"] + view["retrieval"]:
        row["kb"] = names.get(row["kb_id"], "Deleted knowledge base")
    ctx.store.record(sess.email, "trace.view", str(answer_id))
    return view
