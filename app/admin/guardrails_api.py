"""Admin API for guardrail settings (Admin only). Banned topics are edited per department on the Departments screen."""
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import guardrails
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
               if asdict(after)[k] != v and k not in ("blocked_reply", "allow_list")]
    for k in ("blocked_reply", "allow_list"):
        if asdict(before)[k] != asdict(after)[k]:
            changes.append(f"{k} changed")
    ctx.store.record(s.email, "guardrails.update", "guardrails", "; ".join(changes) or "no changes")
    out = guardrails.view(ctx.db)
    out["status"] = guardrails.status()
    return out
