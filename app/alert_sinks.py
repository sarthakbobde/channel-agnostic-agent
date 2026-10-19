"""Optional alert notifications (decision D8): a webhook (Slack-compatible JSON) and/or email. Off by default.

The admin page always shows alert banners. When a sink is switched on, each NEW alert (not its repeats) is also sent out,
in the background, so a slow mail server never slows down an answer. The webhook address and the mail password are secrets:
stored encrypted, never returned, never in a message. Alert text itself never contains a key or connection string.
"""
import json
import logging
import smtplib
import threading
from email.message import EmailMessage

import httpx
from sqlalchemy import select

from app.db import Database, Meta, get_db

log = logging.getLogger(__name__)
META_KEY = "alert_sinks"
OWNER, OWNER_ID = "alert-sink", 0
DEFAULTS = {"webhook_enabled": False, "smtp_enabled": False, "smtp_host": "", "smtp_port": 587, "smtp_user": "", "smtp_from": "",
            "smtp_to": [], "min_severity": "warning"}
SECRETS = ("webhook_url", "smtp_password")


def config(db: Database | None = None) -> dict:
    db = db or get_db()
    with db.session() as s:
        row = s.get(Meta, META_KEY)
        saved = json.loads(row.value) if row and row.value else {}
    return {**DEFAULTS, **saved}


def validate(values: dict) -> dict:
    unknown = sorted(set(values) - set(DEFAULTS) - set(SECRETS))
    if unknown:
        raise ValueError("Unknown settings: " + ", ".join(unknown))
    clean = {}
    for k, v in values.items():
        if k in SECRETS:
            continue
        if k in ("webhook_enabled", "smtp_enabled"):
            if not isinstance(v, bool):
                raise ValueError(f"{k} must be on or off.")
        elif k == "smtp_port":
            if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 65535:
                raise ValueError("The mail port must be a number between 1 and 65535.")
        elif k == "smtp_to":
            if not isinstance(v, list) or any(not isinstance(x, str) or "@" not in x for x in v) or len(v) > 20:
                raise ValueError("Give up to 20 email addresses.")
            v = [x.strip() for x in v]
        elif k == "min_severity":
            if v not in ("warning", "error"):
                raise ValueError("Send alerts of severity warning or error.")
        else:
            if not isinstance(v, str) or len(v) > 254:
                raise ValueError(f"{k} must be text of up to 254 characters.")
            v = v.strip()
        clean[k] = v
    return clean


def secrets_set(db: Database | None = None) -> list[str]:
    from app.knowledge import embeddings
    return embeddings.kb_secret_names(db or get_db(), OWNER_ID, OWNER)


def save(db: Database, values: dict, secrets: dict) -> dict:
    """Validate, then store the settings and (encrypted) secrets."""
    from app.knowledge import embeddings
    clean = validate(values)
    merged = {**config(db), **clean}
    if merged["webhook_enabled"]:
        url = secrets.get("webhook_url") or embeddings.load_kb_secrets(db, OWNER_ID, OWNER).get("webhook_url", "")
        if not url.startswith("https://"):
            raise ValueError("The webhook address must start with https://")
    if merged["smtp_enabled"] and not (merged["smtp_host"] and merged["smtp_from"] and merged["smtp_to"]):
        raise ValueError("Email needs a mail server, a From address and at least one recipient.")
    new_secrets = {k: v for k, v in secrets.items() if k in SECRETS and v}
    if "webhook_url" in new_secrets and not new_secrets["webhook_url"].startswith("https://"):
        raise ValueError("The webhook address must start with https://")
    embeddings.save_kb_secrets(db, OWNER_ID, new_secrets, OWNER)
    with db.session() as s:
        row = s.get(Meta, META_KEY)
        if row is None:
            s.add(Meta(key=META_KEY, value=json.dumps(merged)))
        else:
            row.value = json.dumps(merged)
    return merged


def _send_webhook(url: str, text: str) -> None:
    r = httpx.post(url, json={"text": text}, timeout=10)
    if not r.is_success:
        raise RuntimeError(f"The webhook answered HTTP {r.status_code}.")


def _send_mail(cfg: dict, password: str, subject: str, text: str) -> None:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, cfg["smtp_from"], ", ".join(cfg["smtp_to"])
    msg.set_content(text)
    with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"]), timeout=15) as smtp:
        smtp.starttls()
        if cfg["smtp_user"]:
            smtp.login(cfg["smtp_user"], password)
        smtp.send_message(msg)


def deliver(db: Database, text: str, subject: str, severity: str = "warning") -> dict:
    """Send one message through every switched-on sink. Returns {sink: "sent" | error text}; never raises."""
    from app.knowledge import embeddings
    cfg = config(db)
    if severity == "warning" and cfg["min_severity"] == "error":
        return {}
    secrets = embeddings.load_kb_secrets(db, OWNER_ID, OWNER)
    out = {}
    if cfg["webhook_enabled"] and secrets.get("webhook_url"):
        try:
            _send_webhook(secrets["webhook_url"], text)
            out["webhook"] = "sent"
        except Exception as e:
            out["webhook"] = _scrub(f"{type(e).__name__}", secrets)
    if cfg["smtp_enabled"]:
        try:
            _send_mail(cfg, secrets.get("smtp_password", ""), subject, text)
            out["email"] = "sent"
        except Exception as e:
            out["email"] = _scrub(f"{type(e).__name__}", secrets)
    return out


def _scrub(message: str, secrets: dict) -> str:
    for v in secrets.values():
        if isinstance(v, str) and len(v) >= 4:
            message = message.replace(v, "***")
    return message


def notify(db: Database, alert: dict) -> None:
    """Called when a NEW alert is raised. Runs in the background; failures are logged, never raised."""
    def go():
        try:
            result = deliver(db, f"[{alert['severity']}] {alert['message']}", f"Channel Agnostic Agent alert: {alert['kind']}", alert["severity"])
            failed = {k: v for k, v in result.items() if v != "sent"}
            if failed:
                log.warning("alert notification failed: %s", failed)
        except Exception:
            log.warning("alert notification failed", exc_info=True)
    threading.Thread(target=go, name="alert-sink", daemon=True).start()
