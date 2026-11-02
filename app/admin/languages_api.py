"""Admin API for languages: the message catalogue (D29), the language report (D39) and document summaries (D46).

The catalogue and the report are Admin only. Regenerating one document's summary follows the document rules (Admins
everywhere, Editors in their own departments); summarising existing documents in bulk is Admin only, after an estimate.
Every change is audited, without message text."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from app import catalog, language
from app.admin.api import AdminContext, Session, admin_session, get_ctx
from app.admin.documents_api import _doc_or_404
from app.db import AnswerTrace, GuardrailEvent
from app.knowledge import doc_summary, documents

router = APIRouter(prefix="/admin/api", tags=["admin"])


class TextIn(BaseModel):
    text: str


class BackfillIn(BaseModel):
    kb_id: int | None = None


# ---- message catalogue ---------------------------------------------------------------------------------------------
@router.get("/catalog")
def get_catalog(_: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    return catalog.view(ctx.db)


@router.put("/catalog/{key}/{lang}")
def put_entry(key: str, lang: str, body: TextIn, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    try:
        before, _ = catalog.save(key, lang, body.text, s.email, db=ctx.db)
    except ValueError as e:
        raise HTTPException(422, str(e))
    ctx.store.record(s.email, "catalog.update", f"{key}/{lang}", "approved by saving" + ("" if before else " (new)"))
    return catalog.view(ctx.db)


@router.post("/catalog/{key}/{lang}/approve")
def approve_entry(key: str, lang: str, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    if not catalog.approve(key, lang, s.email, ctx.db):
        raise HTTPException(409, "There is no draft of this message to approve.")
    ctx.store.record(s.email, "catalog.approve", f"{key}/{lang}", "")
    return catalog.view(ctx.db)


@router.post("/catalog/{lang}/draft")
def draft_language(lang: str, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """Draft the missing messages of a language with the preferred AI model. Drafts are not used until approved."""
    if lang not in catalog.languages(ctx.db) or lang == "en":
        raise HTTPException(422, "Add the language under RAG settings > Message languages first.")
    try:
        made = catalog.draft_with_ai(lang, s.email, ctx.db)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(502, f"The AI model could not draft the messages ({type(e).__name__}). Try again, or write them yourself.")
    ctx.store.record(s.email, "catalog.draft", lang, f"{made} drafted by AI")
    return {**catalog.view(ctx.db), "drafted": made}


# ---- the language report -----------------------------------------------------------------------------------------------
@router.get("/reports/languages")
def language_report(days: int = 30, _: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """Per language: answers given, questions refused because the language is not accepted, answers retried or still
    in the wrong language, and questions checked only with the English lists."""
    days = max(1, min(days, 365))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows: dict[str, dict] = {}

    def row(code: str) -> dict:
        return rows.setdefault(code or "", {"code": code or "", "name": language.name(code) if code else "Unknown",
                                            "answers": 0, "rejected": 0, "retried": 0, "wrong": 0, "english_checks": 0, "blocked": 0})
    with ctx.db.session() as s:
        for code, n in s.execute(select(AnswerTrace.language, func.count()).where(AnswerTrace.created_at >= since)
                                 .group_by(AnswerTrace.language)):
            row(code)["answers"] = n
        for check, action, code, n in s.execute(
                select(GuardrailEvent.check, GuardrailEvent.action, GuardrailEvent.language, func.sum(GuardrailEvent.count))
                .where(GuardrailEvent.ts >= since, GuardrailEvent.check.in_(("language", "answer_language", "language_checks")))
                .group_by(GuardrailEvent.check, GuardrailEvent.action, GuardrailEvent.language)):
            r = row(code)
            if check == "language":
                r["rejected"] += n
            elif check == "answer_language":
                r[action if action in ("retried", "wrong") else "retried"] += n
            elif action == "english":
                r["english_checks"] += n
            else:
                r["blocked"] += n
    return {"days": days, "detector": language.status(), "rows": sorted(rows.values(), key=lambda r: (-r["answers"], r["code"]))}


# ---- document summaries --------------------------------------------------------------------------------------------------
@router.post("/documents/{doc_id}/summary")
def regenerate_summary(doc_id: int, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    d = _doc_or_404(ctx, s, doc_id)
    if d["status"] != "Indexed":
        raise HTTPException(409, "A summary is written once the document is indexed.")
    if not doc_summary.schedule(ctx.db, doc_id, force=True):
        raise HTTPException(409, "The summary could not be queued. Try again.")
    ctx.store.record(s.email, "document.summary", d["name"], "regenerate")
    return documents.get(ctx.db, doc_id)


@router.get("/summaries/estimate")
def summaries_estimate(kb_id: int | None = None, _: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    return doc_summary.estimate(ctx.db, kb_id)


@router.post("/summaries/backfill")
def summaries_backfill(body: BackfillIn, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    n = doc_summary.backfill(ctx.db, body.kb_id)
    ctx.store.record(s.email, "document.summary", "existing documents", f"{n} queued")
    return {"queued": n}
