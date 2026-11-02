"""Admin API for glossaries (D22). Admins manage any glossary; an Editor only their own departments' and those departments'
knowledge bases'. Every change (draft saved, CSV imported, draft discarded, published, rolled back) is audited, with counts
and version numbers, not the terms themselves. The test view shows how a question would be expanded, without searching."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app import glossary, scoped_settings
from app.admin.api import AdminContext, Session, admin_session, get_ctx
from app.admin.scoped_settings_api import _editable, _owner, scopes as setting_scopes
from app.knowledge import documents, kbs

router = APIRouter(prefix="/admin/api", tags=["admin"])


class TermsIn(BaseModel):
    terms: list


class CsvIn(BaseModel):
    csv: str


class TestIn(BaseModel):
    kb_id: int
    question: str
    language: str = ""
    draft_scope_type: str | None = None          # use this scope's Draft instead of its published terms
    draft_scope_id: int | None = None


def _scope(ctx: AdminContext, sess: Session, scope_type: str, scope_id: str) -> int:
    if scope_type not in glossary.SCOPES:
        raise HTTPException(422, "A glossary belongs to a knowledge base or a department.")
    _owner(ctx, sess, scope_type, scope_id)
    return int(scope_id)


def _kbs_of(ctx: AdminContext, scope_type: str, scope_id: int) -> list[dict]:
    if scope_type == glossary.KB:
        kb = kbs.get_kb(ctx.db, scope_id)
        return [kb] if kb else []
    return [k for k in kbs.list_kbs(ctx.db) if k["department_id"] == scope_id]


def _view(ctx: AdminContext, scope_type: str, scope_id: int) -> dict:
    out = glossary.view(scope_type, scope_id, ctx.db)
    for part in ("draft", "published"):
        if out[part]:
            out[part]["conflicts"] = glossary.conflicts([glossary.Term(**{k: v for k, v in t.items() if k != "id"}) for t in out[part]["terms"]])
    if scope_type == glossary.KB:
        mode, source = scoped_settings.explain("glossary_mode", kb_id=scope_id, department_id=(kbs.get_kb(ctx.db, scope_id) or {}).get("department_id"), db=ctx.db)
    else:
        mode, source = scoped_settings.explain("glossary_mode", department_id=scope_id, db=ctx.db)
    out.update({"scope_type": scope_type, "scope_id": scope_id, "mode": mode, "mode_from": source,
                "kbs": [{"id": k["id"], "name": k["name"]} for k in _kbs_of(ctx, scope_type, scope_id)],
                "reindex": sum(documents.glossary_stale(ctx.db, k) for k in _kbs_of(ctx, scope_type, scope_id))})
    return out


@router.get("/glossaries/scopes")
def scopes(sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    found = setting_scopes(sess, ctx)
    return {"departments": found["departments"], "kbs": found["kbs"]}


@router.get("/glossaries/{scope_type}/{scope_id}")
def read(scope_type: str, scope_id: str, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    return _view(ctx, scope_type, _scope(ctx, sess, scope_type, scope_id))


@router.post("/glossaries/{scope_type}/{scope_id}/draft")
def start(scope_type: str, scope_id: str, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    sid = _scope(ctx, sess, scope_type, scope_id)
    d = glossary.start_draft(scope_type, sid, sess.email, ctx.db)
    ctx.store.record(sess.email, "glossary.draft", f"{scope_type}:{sid}", f"draft v{d['version']}")
    return _view(ctx, scope_type, sid)


@router.put("/glossaries/{scope_type}/{scope_id}/draft")
def save(scope_type: str, scope_id: str, body: TermsIn, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    sid = _scope(ctx, sess, scope_type, scope_id)
    try:
        before, after = glossary.save_draft(scope_type, sid, body.terms, sess.email, ctx.db)
    except ValueError as e:
        raise HTTPException(422, str(e))
    ctx.store.record(sess.email, "glossary.draft", f"{scope_type}:{sid}", f"draft saved: {before} -> {after} terms")
    return _view(ctx, scope_type, sid)


@router.post("/glossaries/{scope_type}/{scope_id}/import")
def import_csv(scope_type: str, scope_id: str, body: CsvIn, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """Replace the Draft's terms with a CSV's (the Published version is untouched until the Draft is published)."""
    sid = _scope(ctx, sess, scope_type, scope_id)
    try:
        before, after = glossary.save_draft(scope_type, sid, glossary.from_csv(body.csv), sess.email, ctx.db)
    except (ValueError, KeyError) as e:
        raise HTTPException(422, str(e))
    ctx.store.record(sess.email, "glossary.import", f"{scope_type}:{sid}", f"CSV imported into the draft: {before} -> {after} terms")
    return _view(ctx, scope_type, sid)


@router.get("/glossaries/{scope_type}/{scope_id}/export")
def export_csv(scope_type: str, scope_id: str, version_id: int | None = None, sess: Session = Depends(admin_session),
               ctx: AdminContext = Depends(get_ctx)):
    sid = _scope(ctx, sess, scope_type, scope_id)
    v = glossary.view(scope_type, sid, ctx.db)
    chosen = next((x for x in v["versions"] if x["id"] == version_id), None) if version_id else (v["published"] or v["draft"])
    if not chosen:
        raise HTTPException(404, "This glossary has no terms yet.")
    name = f"glossary-{scope_type}-{sid}-v{chosen['version']}.csv"
    return Response(glossary.to_csv(glossary.version_terms(chosen["id"], ctx.db)), media_type="text/csv",
                     headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.delete("/glossaries/{scope_type}/{scope_id}/draft")
def discard(scope_type: str, scope_id: str, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    sid = _scope(ctx, sess, scope_type, scope_id)
    if glossary.discard_draft(scope_type, sid, ctx.db):
        ctx.store.record(sess.email, "glossary.discard", f"{scope_type}:{sid}", "draft discarded")
    return _view(ctx, scope_type, sid)


def _version(ctx: AdminContext, sess: Session, version_id: int) -> tuple[str, int]:
    found = glossary.version_scope(version_id, ctx.db)
    if found is None:
        raise HTTPException(404, "No such glossary version.")
    _owner(ctx, sess, found[0], str(found[1]))
    return found[0], found[1]


@router.post("/glossaries/versions/{version_id}/publish")
def publish(version_id: int, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    scope_type, sid = _version(ctx, sess, version_id)
    v = glossary.view(scope_type, sid, ctx.db)
    draft = v["draft"]
    if draft and draft["id"] == version_id and glossary.conflicts([glossary.Term(**{k: x for k, x in t.items() if k != "id"}) for t in draft["terms"]]):
        raise HTTPException(409, "Resolve the conflicts listed under the draft before publishing.")
    try:
        after, before = glossary.publish(version_id, sess.email, ctx.db)
    except ValueError as e:
        raise HTTPException(409, str(e))
    ctx.store.record(sess.email, "glossary.publish", f"{scope_type}:{sid}", f"v{after['version']} published" + (f", v{before['version']} archived" if before else ""))
    return _view(ctx, scope_type, sid)


@router.post("/glossaries/versions/{version_id}/rollback")
def roll_back(version_id: int, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    scope_type, sid = _version(ctx, sess, version_id)
    try:
        after, before = glossary.rollback(version_id, sess.email, ctx.db)
    except ValueError as e:
        raise HTTPException(409, str(e))
    ctx.store.record(sess.email, "glossary.rollback", f"{scope_type}:{sid}", f"back to v{after['version']}" + (f" (was v{before['version']})" if before else ""))
    return _view(ctx, scope_type, sid)


@router.post("/glossaries/test")
def test(body: TestIn, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """How a question would be expanded for a knowledge base. Nothing is searched and no model is called."""
    kb = kbs.get_kb(ctx.db, body.kb_id)
    mine = _editable(ctx, sess)
    if kb is None or (mine is not None and kb["department_id"] not in mine):
        raise HTTPException(404, "No such knowledge base.")
    draft = None
    if body.draft_scope_type:
        sid = _scope(ctx, sess, body.draft_scope_type, str(body.draft_scope_id))
        d = glossary.view(body.draft_scope_type, sid, ctx.db)["draft"]
        if d:
            draft = (body.draft_scope_type, sid, glossary.version_terms(d["id"], ctx.db))
    terms, versions = glossary.terms_for(kb, ctx.db, draft=draft)
    e = glossary.expand_with(" ".join(body.question.split())[:2000], terms, body.language.strip().lower(), versions=versions)
    return {**e.as_dict(), "mode": glossary.mode(kb)}
