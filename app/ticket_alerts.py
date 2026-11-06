"""Alerts about tickets (Release 3, Phase 2; D58).

Where an alert goes (`ticket_alert_channels`) and which events send one (`ticket_alert_events`) are settings, and a department can
override both. Each channel is an adapter; a failing channel never blocks the others or the ticket, and nothing here ever raises.

An alert holds the ticket number and a link, never the evidence, the title or any text a person wrote.

Channels:
* `admin_screen`: an alert banner in the admin center, for the person it concerns (and every Admin sees all alerts).
* `email`: a short mail through the SMTP settings of the alert sinks. Nothing is sent if SMTP is not switched on.
A chat-app channel is not built yet: it needs new permissions in Slack and Teams, so it is left for later.
"""
import logging
from email.message import EmailMessage

from sqlalchemy import select

from app import alerts, global_settings, scoped_settings, tickets
from app.config import settings as env
from app.db import Database, DepartmentEditor, GroupMember, Ticket, User, get_db

log = logging.getLogger(__name__)
ASSIGNED, APPROVAL, EVIDENCE_ADDED, QUEUE_RELEASED = "assigned", "approval", "evidence_added", "queue_released"
TEXTS = {
    ASSIGNED: "Ticket #{id} is waiting for you.",
    APPROVAL: "A proposal on ticket #{id} is waiting for your approval.",
    EVIDENCE_ADDED: "New evidence was added to ticket #{id}.",
}


def link(ticket_id: int) -> str:
    return f"{(env.admin_public_url or '').rstrip('/')}/admin/?ticket={ticket_id}"


def configured(db: Database, department_id: int | None) -> tuple[tuple, tuple]:
    """(channels, events) in effect for a department: its own override, else the global setting."""
    channels = scoped_settings.get("ticket_alert_channels", department_id=department_id, db=db)
    events = scoped_settings.get("ticket_alert_events", department_id=department_id, db=db)
    return tuple(channels or ()), tuple(events or ())


def recipients(db: Database, t: Ticket, event: str) -> list[str]:
    """The people an event concerns: the person it is assigned to, the members of the group it is assigned to, or (the department's
    queue, and approvals) the Editors of the ticket's department. Active people with an email address only."""
    with db.session() as s:
        ids: set[int] = set()
        if event != APPROVAL and t.assignee_type == tickets.USER and t.assignee_id:
            ids.add(t.assignee_id)
        elif event != APPROVAL and t.assignee_type == tickets.GROUP and t.assignee_id:
            ids |= set(s.scalars(select(GroupMember.user_id).where(GroupMember.group_id == t.assignee_id)))
        if not ids and t.department_id is not None:
            ids |= set(s.scalars(select(DepartmentEditor.user_id).where(DepartmentEditor.department_id == t.department_id)))
        if not ids:
            return []
        return sorted({u.email for u in s.scalars(select(User).where(User.id.in_(ids), User.status == "Active")) if u.email})


def _admin_screen(db: Database, event: str, t: Ticket, people: list[str]) -> str:
    text = TEXTS[event].format(id=t.id) + " Open Tickets in the admin center."
    for email in people or [""]:                         # no one to name: an alert every Admin sees
        alerts.raise_alert(db, f"ticket:{event}:{t.id}:{email}", "ticket", text, "info", email)
    return "sent"


def _email(db: Database, event: str, t: Ticket, people: list[str]) -> str:
    from app import alert_sinks
    from app.knowledge import embeddings
    cfg = alert_sinks.config(db)
    if not cfg["smtp_enabled"] or not people:
        return "skipped"
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = f"Ticket #{t.id}", cfg["smtp_from"], ", ".join(people)
    msg.set_content(TEXTS[event].format(id=t.id) + "\n" + link(t.id))
    import smtplib
    password = embeddings.load_kb_secrets(db, alert_sinks.OWNER_ID, alert_sinks.OWNER).get("smtp_password", "")
    with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"]), timeout=15) as smtp:
        smtp.starttls()
        if cfg["smtp_user"]:
            smtp.login(cfg["smtp_user"], password)
        smtp.send_message(msg)
    return "sent"


ADAPTERS = {"admin_screen": _admin_screen, "email": _email}


def notify(db: Database | None, event: str, ticket_id: int) -> dict:
    """Send the alert for `event` on a ticket through every channel that is on, if the event is on. -> {channel: sent | skipped | error}.
    Never raises."""
    db = db or get_db()
    out: dict = {}
    try:
        with db.session() as s:
            t = s.get(Ticket, ticket_id)
            if t is None:
                return out
            s.expunge(t)
        channels, events = configured(db, t.department_id)
        if event not in events or event not in TEXTS:
            return out
        people = recipients(db, t, event)
        for name in channels:
            adapter = ADAPTERS.get(name)
            if adapter is None:
                continue
            try:
                out[name] = adapter(db, event, t, people)
            except Exception as e:                       # one failing channel never blocks the others or the ticket
                out[name] = type(e).__name__
                log.warning("ticket alert through %s failed (%s)", name, type(e).__name__)
    except Exception:
        log.warning("could not send a ticket alert", exc_info=True)
    return out


def queue_released(db: Database | None, ticket_ids: list[int]) -> dict:
    """One digest alert when the agent's queue is released to people (D63), not one per ticket."""
    db = db or get_db()
    out: dict = {}
    try:
        if not ticket_ids:
            return out
        channels, events = configured(db, None)
        if QUEUE_RELEASED not in events:
            return out
        text = f"{len(ticket_ids)} ticket(s) were released from the agent's queue to people. Open Tickets in the admin center."
        if "admin_screen" in channels:
            alerts.raise_alert(db, "ticket:queue_released", "ticket", text, "info", "")
            out["admin_screen"] = "sent"
    except Exception:
        log.warning("could not send the queue digest", exc_info=True)
    return out
