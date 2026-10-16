"""Usage recording: one row per model call attempt. Cost is filled in only when the model has a
hand-entered price (decision D6); otherwise it stays empty rather than guessing."""
import logging

from sqlalchemy import select

from app.db import Database, ModelPrice, UsageRecord

log = logging.getLogger(__name__)


def record(db: Database, *, provider: str, provider_type: str, model: str, purpose: str,
           department: str | None, input_tokens: int | None, output_tokens: int | None,
           status: str, error: str = "", kb_id: int | None = None) -> None:
    """Never raises: failing to record usage must not break an answer."""
    try:
        with db.session() as s:
            cost = None
            price = s.scalar(select(ModelPrice).where(ModelPrice.provider_type == provider_type,
                                                      ModelPrice.model == model))
            if price is not None and input_tokens is not None and output_tokens is not None:
                cost = input_tokens / 1000 * price.input_per_1k + output_tokens / 1000 * price.output_per_1k
            s.add(UsageRecord(provider=provider, model=model, department=department, kb_id=kb_id,
                              purpose=purpose, input_tokens=input_tokens, output_tokens=output_tokens,
                              cost=cost, status=status, error=error[:300]))
    except Exception:
        log.warning("could not record model usage", exc_info=True)
