"""Alerts: conditions an admin should know about (shown as banners in the admin page).

Repeats of the same `key` update one open row instead of piling up, and a condition that clears
(for example a provider that works again) can be resolved automatically.
"""
from datetime import datetime, timezone

from sqlalchemy import select

from app.db import Alert, Database


def _now() -> datetime:
    return datetime.now(timezone.utc)


def raise_alert(db: Database, key: str, kind: str, message: str, severity: str = "warning") -> None:
    with db.session() as s:
        row = s.scalar(select(Alert).where(Alert.key == key, Alert.resolved.is_(False)))
        if row is None:
            s.add(Alert(key=key, kind=kind, message=message, severity=severity))
        else:
            row.count += 1
            row.last_seen = _now()
            row.message = message
            row.severity = severity


def resolve_key(db: Database, key: str) -> None:
    """Close the open alert for `key`, if any."""
    with db.session() as s:
        row = s.scalar(select(Alert).where(Alert.key == key, Alert.resolved.is_(False)))
        if row is not None:
            row.resolved, row.resolved_at = True, _now()


def resolve_id(db: Database, alert_id: int) -> bool:
    with db.session() as s:
        row = s.get(Alert, alert_id)
        if row is None or row.resolved:
            return False
        row.resolved, row.resolved_at = True, _now()
        return True


def active(db: Database) -> list[dict]:
    with db.session() as s:
        rows = s.scalars(select(Alert).where(Alert.resolved.is_(False)).order_by(Alert.last_seen.desc()))
        return [{"id": a.id, "key": a.key, "kind": a.kind, "message": a.message, "severity": a.severity,
                 "count": a.count, "first_seen": a.first_seen, "last_seen": a.last_seen} for a in rows]
