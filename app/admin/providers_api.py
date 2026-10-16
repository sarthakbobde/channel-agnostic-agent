"""Admin API for LLM providers, the approved-provider list, model settings and alerts.

Same rules as the channel API: test before saving (nothing is stored if the test fails), the API key
is write-only, and every change is audited. Changes reach the answering code within seconds.
"""
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import alerts, llm
from app.admin.api import AdminContext, Session, admin_session, get_ctx
from app.providers import REGISTRY, ProviderError, adapter_class
from app.providers.store import ProviderStore

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/api", tags=["admin"])


def _store(ctx: AdminContext) -> ProviderStore:
    return ProviderStore(ctx.db, ctx.store.secrets)


def _type_info(type_: str, approved: list[str]) -> dict:
    cls = adapter_class(type_)
    return {"type": type_, "title": cls.title, "warning": cls.warning, "approved": type_ in approved,
            "fields": [{"name": f.name, "label": f.label, "kind": f.kind, "secret": f.secret,
                        "required": f.required, "help": f.help, **({"value": f.default} if not f.secret else {"is_set": False})}
                       for f in cls.SPEC]}


def _check(type_: str, cfg: dict) -> list[str]:
    """Validate and test a config against the real provider. Returns its models or raises HTTPException."""
    adapter = adapter_class(type_)
    problems = adapter.validate(cfg)
    if problems:
        raise HTTPException(422, "; ".join(problems))
    try:
        models = adapter(cfg).list_models()
    except ProviderError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.warning("provider test failed unexpectedly: %s", type(e).__name__)
        raise HTTPException(400, f"The provider test failed ({type(e).__name__}).")
    if not models:
        raise HTTPException(400, "The provider answered but lists no models for this key.")
    return models


def _value_error(e: ValueError) -> HTTPException:
    return HTTPException(422, str(e))


# ---- providers ----------------------------------------------------------------------------
class ProviderBody(BaseModel):
    type: str | None = None
    name: str | None = None
    values: dict[str, Any] = {}


class ApprovalBody(BaseModel):
    approved: list[str]


@router.get("/providers")
def list_providers(_: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    store = _store(ctx)
    approved = store.approved()
    return {"types": [_type_info(t, approved) for t in REGISTRY], "providers": store.list_all(), "approved": approved,
            "model_settings": store.model_settings()}


@router.put("/providers/approval")
def set_approval(body: ApprovalBody, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    try:
        approved = _store(ctx).set_approved(body.approved, s.email)
    except ValueError as e:
        raise _value_error(e)
    return {"approved": approved}


@router.post("/providers/test")
def test_config(body: ProviderBody, _: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """Dry run: nothing is saved."""
    if not body.type or body.type not in REGISTRY:
        raise HTTPException(422, "Choose a provider type.")
    try:
        cfg = _store(ctx).merged(body.type, body.values)
    except ValueError as e:
        raise _value_error(e)
    models = _check(body.type, cfg)
    return {"ok": True, "message": f"Connected. {len(models)} model(s) available.", "models": models}


@router.post("/providers", status_code=201)
def create_provider(body: ProviderBody, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    store = _store(ctx)
    if not body.type or body.type not in REGISTRY:
        raise HTTPException(422, "Choose a provider type.")
    if body.type not in store.approved():
        raise HTTPException(422, f"{adapter_class(body.type).title} is not an approved provider. Approve it first.")
    try:
        cfg = store.merged(body.type, body.values)
    except ValueError as e:
        raise _value_error(e)
    models = _check(body.type, cfg)
    try:
        pid = store.create(body.type, body.name or adapter_class(body.type).title, body.values, models, s.email)
    except ValueError as e:
        raise _value_error(e)
    llm.invalidate()
    return store.get(pid)


@router.put("/providers/{pid}")
def update_provider(pid: int, body: ProviderBody, s: Session = Depends(admin_session),
                    ctx: AdminContext = Depends(get_ctx)):
    store = _store(ctx)
    current = store.get(pid)
    if current is None:
        raise HTTPException(404, "No such provider.")
    try:
        cfg = store.merged(current["type"], body.values, pid)
    except ValueError as e:
        raise _value_error(e)
    models = _check(current["type"], cfg)
    try:
        store.update(pid, body.name, body.values, models, s.email)
    except ValueError as e:
        raise _value_error(e)
    llm.invalidate()
    return store.get(pid)


@router.post("/providers/{pid}/test")
def retest_provider(pid: int, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    store = _store(ctx)
    found = store.runtime(pid)
    if found is None:
        raise HTTPException(404, "No such provider.")
    info, cfg = found
    try:
        models = _check(info["type"], cfg)
    except HTTPException as e:
        store.set_status(pid, "Failed", str(e.detail), actor=s.email)
        llm.invalidate()
        return {"ok": False, "message": str(e.detail), "models": []}
    # Listing models proves the key and the address, not that the account can still answer, so this does
    # NOT clear a 'credit exhausted' alert: only a real successful answer does (or an admin dismissing it).
    store.set_status(pid, "Connected", "", models, actor=s.email)
    llm.invalidate()
    return {"ok": True, "message": f"Connected. {len(models)} model(s) available.", "models": models}


@router.delete("/providers/{pid}")
def delete_provider(pid: int, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    try:
        _store(ctx).delete(pid, s.email)
    except ValueError as e:
        raise HTTPException(409, str(e))
    llm.invalidate()
    return {"ok": True}


# ---- model settings ---------------------------------------------------------------------------
class Choice(BaseModel):
    provider_id: int | None = None
    model: str = ""


class ModelSettingsBody(BaseModel):
    preferred: Choice
    fallback: Choice | None = None
    request_timeout: int


@router.get("/model-settings")
def get_model_settings(_: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    return _store(ctx).model_settings() or {"preferred": None, "fallback": None, "request_timeout": 60}


@router.put("/model-settings")
def put_model_settings(body: ModelSettingsBody, s: Session = Depends(admin_session),
                       ctx: AdminContext = Depends(get_ctx)):
    store = _store(ctx)
    try:
        warnings = store.set_model_settings(body.preferred.model_dump(),
                                            body.fallback.model_dump() if body.fallback else None,
                                            body.request_timeout, s.email)
    except ValueError as e:
        raise _value_error(e)
    llm.invalidate()
    return {**store.model_settings(), "warnings": warnings}


# ---- alerts -------------------------------------------------------------------------------------
@router.get("/alerts")
def list_alerts(_: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    return alerts.active(ctx.db)


@router.post("/alerts/{alert_id}/resolve")
def resolve_alert(alert_id: int, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    if not alerts.resolve_id(ctx.db, alert_id):
        raise HTTPException(404, "No such open alert.")
    ctx.store.record(s.email, "alert.resolve", str(alert_id))
    return {"ok": True}
