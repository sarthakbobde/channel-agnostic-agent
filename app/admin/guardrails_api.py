"""Admin API for guardrail settings (Admin only). Banned topics are edited per department on the Departments screen."""
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import guardrails, pii
from app.admin.api import AdminContext, Session, admin_session, get_ctx

router = APIRouter(prefix="/admin/api", tags=["admin"])


class Update(BaseModel):
    values: dict


@router.get("/guardrails")
def get_settings(_: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    out = guardrails.view(ctx.db)
    out["status"] = guardrails.status()
    return out


@router.put("/guardrails")
def put_settings(body: Update, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    try:
        before, after = guardrails.update(body.values, s.email, ctx.db)
    except ValueError as e:
        raise HTTPException(422, str(e))
    changes = [f"{k}: {v} -> {asdict(after)[k]}" for k, v in asdict(before).items()
               if asdict(after)[k] != v and k not in ("blocked_reply", "allow_list", "sensitive_keywords", "language_lists")]
    for k in ("blocked_reply", "allow_list", "sensitive_keywords", "language_lists"):
        if asdict(before)[k] != asdict(after)[k]:
            changes.append(f"{k} changed")
    ctx.store.record(s.email, "guardrails.update", "guardrails", "; ".join(changes) or "no changes")
    out = guardrails.view(ctx.db)
    out["status"] = guardrails.status()
    return out


# ---- personal-data types an Admin defines, and a Try it box --------------------------------------------------------
class PiiType(BaseModel):
    name: str
    pattern: str
    context: list[str] = []
    confidence: int = 60


class Sample(BaseModel):
    text: str
    pattern: str | None = None          # try a pattern before saving it


@router.get("/guardrails/pii-types")
def list_pii_types(_: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    return {"types": pii.list_custom(ctx.db)}


@router.post("/guardrails/pii-types", status_code=201)
def add_pii_type(body: PiiType, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    try:
        t = pii.save_custom(ctx.db, body.model_dump(), s.email)
    except LookupError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    ctx.store.record(s.email, "pii_type.create", t["name"], f"pattern {t['pattern']}; confidence {t['confidence']}")
    return t


@router.put("/guardrails/pii-types/{type_id}")
def change_pii_type(type_id: int, body: PiiType, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    try:
        t = pii.save_custom(ctx.db, body.model_dump(), s.email, type_id)
    except LookupError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(404 if "No such" in str(e) else 422, str(e))
    ctx.store.record(s.email, "pii_type.update", t["name"], f"pattern {t['pattern']}; confidence {t['confidence']}")
    return t


@router.delete("/guardrails/pii-types/{type_id}")
def remove_pii_type(type_id: int, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    try:
        t = pii.delete_custom(ctx.db, type_id, s.email)
    except ValueError as e:
        raise HTTPException(404, str(e))
    ctx.store.record(s.email, "pii_type.delete", t["name"], "")
    return {"ok": True}


@router.post("/guardrails/pii-test")
def try_pii(body: Sample, _: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """What the saved settings would make of a sample line, or what a draft pattern matches in it. Nothing is stored
    or logged."""
    if len(body.text) > 2000:
        raise HTTPException(422, "Try it with up to 2,000 characters.")
    if body.pattern is not None:
        try:
            return {"matches": pii.try_pattern(body.pattern, body.text)}
        except ValueError as e:
            raise HTTPException(422, str(e))
    snap = guardrails.current(ctx.db)
    masked, counts = guardrails.mask_with(body.text, snap)
    return {"masked": masked, "counts": counts}



# ---- PII protection (12g): the master switch, changed with a reason --------------------------------------------
class Protection(BaseModel):
    enabled: bool
    reason: str


@router.get("/guardrails/pii-protection")
def get_protection(_: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    from app.knowledge import pii_access
    return {"enabled": pii_access.master(ctx.db), "unprotected": pii_access.unprotected(ctx.db)}


@router.put("/guardrails/pii-protection")
def put_protection(body: Protection, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    from app.knowledge import pii_access
    try:
        pii_access.set_master(ctx.db, body.enabled, body.reason, s.email)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"enabled": pii_access.master(ctx.db), "unprotected": pii_access.unprotected(ctx.db)}
