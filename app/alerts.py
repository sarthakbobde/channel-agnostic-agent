"""Alerts: conditions an admin should know about (shown as banners in the admin page).

Repeats of the same `key` update one open row instead of piling up, and a condition that clears
(for example a provider that works again) can be resolved automatically.
"""
from datetime import datetime, timezone

from sqlalchemy import select

from app.db import Alert, Database


def _now() -> datetime:
    return datetime.now(timezone.utc)


def raise_alert(db: Database, key: str, kind: str, message: str, severity: str = "warning", audience: str = "") -> None:
    """Open an alert, or count a repeat of one that is already open. A NEW alert is also sent to any switched-on alert sink."""
    is_new = False
    with db.session() as s:
        row = s.scalar(select(Alert).where(Alert.key == key, Alert.resolved.is_(False)))
        if row is None:
            s.add(Alert(key=key, kind=kind, message=message, severity=severity, audience=audience))
            is_new = True
        else:
            row.count += 1
            row.last_seen = _now()
            row.message = message
            row.severity = severity
    if is_new:
        try:
            from app import alert_sinks
            alert_sinks.notify(db, {"kind": kind, "message": message, "severity": severity})
        except Exception:
            pass                                   # notifying is best effort; the banner is the record


def resolve_key(db: Database, key: str) -> None:
    """Close the open alert for `key`, if any."""
    with db.session() as s:
        row = s.scalar(select(Alert).where(Alert.key == key, Alert.resolved.is_(False)))
        if row is not None:
            row.resolved, row.resolved_at = True, _now()


def resolve_id(db: Database, alert_id: int, audience: str | None = None) -> bool:
    """Close an alert. `audience` (an email) limits it to alerts meant for that person; None = no limit (an Admin)."""
    with db.session() as s:
        row = s.get(Alert, alert_id)
        if row is None or row.resolved or (audience is not None and row.audience != audience):
            return False
        row.resolved, row.resolved_at = True, _now()
        return True


def active(db: Database, audience: str | None = None) -> list[dict]:
    """Open alerts. `audience=None` is the Admin view (all); an email returns only the alerts meant for that person."""
    with db.session() as s:
        q = select(Alert).where(Alert.resolved.is_(False))
        if audience is not None:
            q = q.where(Alert.audience == audience)
        rows = s.scalars(q.order_by(Alert.last_seen.desc()))
        return [{"id": a.id, "key": a.key, "kind": a.kind, "message": a.message, "severity": a.severity,
                 "count": a.count, "first_seen": a.first_seen, "last_seen": a.last_seen, "audience": a.audience} for a in rows]
