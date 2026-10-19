"""Admin API for the optional alert notifications (webhook and email). Off by default; Admin only."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import alert_sinks
from app.admin.api import AdminContext, Session, admin_session, get_ctx

router = APIRouter(prefix="/admin/api", tags=["admin"])


class SinkIn(BaseModel):
    values: dict = {}
    secrets: dict = {}


def _view(ctx: AdminContext) -> dict:
    return {**alert_sinks.config(ctx.db), "secrets_set": alert_sinks.secrets_set(ctx.db)}


@router.get("/alert-sinks")
def get_sinks(_: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    return _view(ctx)


@router.put("/alert-sinks")
def put_sinks(body: SinkIn, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    before = alert_sinks.config(ctx.db)
    try:
        after = alert_sinks.save(ctx.db, body.values, body.secrets)
    except ValueError as e:
        raise HTTPException(422, str(e))
    changes = [f"{k}: {before[k]} -> {after[k]}" for k in after if after[k] != before.get(k) and k != "smtp_user"]
    changes += [f"{k} replaced" for k in body.secrets if body.secrets.get(k)]
    ctx.store.record(s.email, "alert_sinks.update", "alert notifications", "; ".join(changes) or "no changes")
    return _view(ctx)


@router.post("/alert-sinks/test")
def test_sinks(s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """Send a test message through whatever is switched on."""
    result = alert_sinks.deliver(ctx.db, "[test] This is a test alert from Channel Agnostic Agent.", "Channel Agnostic Agent test alert", "error")
    ctx.store.record(s.email, "alert_sinks.test", "alert notifications", ", ".join(f"{k}: {v}" for k, v in result.items()) or "nothing switched on")
    return {"results": result, "message": "Nothing is switched on." if not result else ""}
