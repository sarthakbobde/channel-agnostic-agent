"""Admin API for usage and cost, and the price list.

Usage rows come from every model call (answers, routing, summaries, previews) and every embedding call. Cost is filled in only
when the model has a hand-entered price (decision D6); calls without one are counted as "unpriced" so a total is never mistaken
for the whole truth. Admins see everything; an Editor sees only their own departments' usage. Prices are Admin only.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import case, func, select

from app.admin.api import AdminContext, Session, admin_session, get_ctx
from app.admin.kbs_api import _my_departments
from app.db import KnowledgeBase, ModelPrice, UsageRecord

router = APIRouter(prefix="/admin/api", tags=["admin"])
GROUPS = ("provider", "model", "department", "kb", "purpose", "kind", "day", "month")


def _day(text: str, end: bool = False):
    if not text:
        return None
    try:
        d = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(422, "Dates look like 2026-10-31.")
    return d + timedelta(days=1) if end else d


def _bucket(db, fmt: str):
    """A SQL expression that turns the timestamp into 'YYYY-MM-DD' or 'YYYY-MM' on SQLite and PostgreSQL."""
    if db.engine.dialect.name == "postgresql":
        return func.to_char(UsageRecord.ts, "YYYY-MM-DD" if fmt == "day" else "YYYY-MM")
    return func.strftime("%Y-%m-%d" if fmt == "day" else "%Y-%m", UsageRecord.ts)


@router.get("/usage")
def usage(group: str = "provider", date_from: str = "", date_to: str = "", kind: str = "", department: str = "",
          sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    if group not in GROUPS:
        raise HTTPException(422, "Group by one of: " + ", ".join(GROUPS) + ".")
    db = ctx.db
    cols = {"provider": UsageRecord.provider, "model": UsageRecord.model, "department": func.coalesce(UsageRecord.department, ""),
            "kb": func.coalesce(UsageRecord.kb_id, 0), "purpose": UsageRecord.purpose, "kind": UsageRecord.kind,
            "day": _bucket(db, "day"), "month": _bucket(db, "month")}
    key = cols[group]
    ok_call = case((UsageRecord.status == "ok", 1), else_=0)
    unpriced = case(((UsageRecord.status == "ok") & UsageRecord.cost.is_(None) & (UsageRecord.kind == "llm"), 1),
                    ((UsageRecord.status == "ok") & UsageRecord.cost.is_(None) & (UsageRecord.kind == "embedding"), 1), else_=0)
    q = select(key.label("k"), func.count().label("calls"), func.sum(ok_call).label("ok"),
               func.coalesce(func.sum(UsageRecord.input_tokens), 0).label("tin"), func.coalesce(func.sum(UsageRecord.output_tokens), 0).label("tout"),
               func.coalesce(func.sum(UsageRecord.cost), 0.0).label("cost"), func.sum(unpriced).label("unpriced")).group_by(key)
    where = []
    if _day(date_from):
        where.append(UsageRecord.ts >= _day(date_from))
    if _day(date_to, True):
        where.append(UsageRecord.ts < _day(date_to, True))
    if kind:
        where.append(UsageRecord.kind == kind)
    if department:
        where.append(UsageRecord.department == department)
    if sess.role != "admin":                                           # Editors: their own departments only
        mine = [d["name"] for d in _my_departments(ctx, sess)]
        where.append(UsageRecord.department.in_(mine or [""]))
    if where:
        q = q.where(*where)
    with db.session() as s:
        names = dict(s.execute(select(KnowledgeBase.id, KnowledgeBase.name)).all()) if group == "kb" else {}
        rows = s.execute(q).all()
    out = []
    for r in rows:
        label = (names.get(r.k, "No knowledge base" if not r.k else f"Deleted knowledge base {r.k}") if group == "kb"
                 else (r.k or ("No department" if group == "department" else "–")))
        out.append({"key": r.k, "label": str(label), "calls": r.calls, "ok_calls": int(r.ok or 0), "failed_calls": r.calls - int(r.ok or 0),
                    "input_tokens": int(r.tin), "output_tokens": int(r.tout), "cost": round(float(r.cost), 6), "unpriced_calls": int(r.unpriced or 0)})
    out.sort(key=(lambda x: x["label"]) if group in ("day", "month") else (lambda x: -x["cost"] if x["cost"] else -x["input_tokens"]))
    totals = {k: (round(sum(x[k] for x in out), 6) if k == "cost" else sum(x[k] for x in out))
              for k in ("calls", "ok_calls", "failed_calls", "input_tokens", "output_tokens", "cost", "unpriced_calls")}
    return {"group": group, "rows": out, "totals": totals, "scope": "all departments" if sess.role == "admin" else "your departments"}


# ---- price list (Admin only) -------------------------------------------------------------------------------
class PriceIn(BaseModel):
    provider_type: str
    model: str
    input_per_1k: float
    output_per_1k: float = 0.0


def _price_view(p: ModelPrice) -> dict:
    return {"id": p.id, "provider_type": p.provider_type, "model": p.model, "input_per_1k": p.input_per_1k,
            "output_per_1k": p.output_per_1k, "updated_on": p.updated_on}


@router.get("/prices")
def prices(_: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """The price list, and the models that have been used but have no price (so their cost shows as unpriced)."""
    with ctx.db.session() as s:
        listed = [_price_view(p) for p in s.scalars(select(ModelPrice).order_by(ModelPrice.provider_type, ModelPrice.model))]
        have = {(p["provider_type"], p["model"]) for p in listed}
        seen = s.execute(select(UsageRecord.provider, UsageRecord.model, UsageRecord.kind, func.count(), func.sum(case((UsageRecord.cost.is_(None), 1), else_=0)))
                         .where(UsageRecord.status == "ok").group_by(UsageRecord.provider, UsageRecord.model, UsageRecord.kind)).all()
    unpriced = [{"provider": p, "model": m, "kind": k, "calls": n, "unpriced_calls": int(u or 0)} for p, m, k, n, u in seen if u]
    return {"prices": listed, "unpriced": unpriced}


@router.put("/prices")
def set_price(body: PriceIn, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    ptype, model = body.provider_type.strip(), body.model.strip()
    if not ptype or not model or len(ptype) > 32 or len(model) > 200:
        raise HTTPException(422, "Give the provider type and the model name.")
    if min(body.input_per_1k, body.output_per_1k) < 0 or max(body.input_per_1k, body.output_per_1k) > 1000:
        raise HTTPException(422, "Prices are per 1,000 tokens, between 0 and 1000.")
    with ctx.db.session() as s:
        p = s.scalar(select(ModelPrice).where(ModelPrice.provider_type == ptype, ModelPrice.model == model))
        before = f"{p.input_per_1k}/{p.output_per_1k}" if p else "none"
        if p is None:
            p = ModelPrice(provider_type=ptype, model=model, input_per_1k=body.input_per_1k, output_per_1k=body.output_per_1k)
            s.add(p)
        else:
            p.input_per_1k, p.output_per_1k = body.input_per_1k, body.output_per_1k
            p.updated_on = datetime.now(timezone.utc)
        s.flush()
        view = _price_view(p)
    ctx.store.record(sess.email, "price.set", f"{ptype} / {model}", f"{before} -> {body.input_per_1k}/{body.output_per_1k} per 1k tokens (input/output)")
    return view


@router.delete("/prices/{price_id}")
def delete_price(price_id: int, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    with ctx.db.session() as s:
        p = s.get(ModelPrice, price_id)
        if p is None:
            raise HTTPException(404, "No such price.")
        label = f"{p.provider_type} / {p.model}"
        s.delete(p)
    ctx.store.record(sess.email, "price.delete", label)
    return {"ok": True}
