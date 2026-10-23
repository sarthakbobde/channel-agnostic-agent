"""People: Admins and Editors (who sign in to the admin panel) and Viewers (people the assistant
answers). Password sign-in, plus Microsoft or Google single sign-on for people already in the list (sso.py)."""
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select

from app.admin.auth import dummy_verify, hash_password, verify_password
from app.db import Database, Department, DepartmentEditor, SetPasswordToken, User

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 12
ROLES = ("admin", "editor", "viewer")
PANEL_ROLES = ("admin", "editor")          # roles that can sign in to the admin panel
STATUSES = ("Active", "Deactivated")
LINK_HOURS = 24


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_email(email: str, domain: str = "") -> str:
    e = normalize_email(email)
    if not _EMAIL.match(e):
        raise ValueError("Enter a valid email address.")
    if domain and not e.endswith("@" + domain.lower()):
        raise ValueError(f"Only @{domain} addresses can be added.")
    return e


def validate_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise ValueError(f"Password must be at least {MIN_PASSWORD} characters.")


def _view(u: User) -> dict:
    return {"id": u.id, "email": u.email, "name": u.name, "role": u.role, "department_id": u.department_id,
            "status": u.status, "provider": u.provider, "created_at": u.created_at,
            "last_sign_in": u.last_sign_in, "password_hash": u.password_hash,
            "sso_microsoft": u.sso_microsoft, "sso_google": u.sso_google, "session_version": u.session_version or 0}


SSO_PROVIDERS = ("microsoft", "google")
_PRIVATE = ("password_hash", "sso_microsoft", "sso_google", "session_version")


def sign_in_methods(u: dict) -> list[str]:
    """How a person can sign in: "local" (email and password), "microsoft", "google"."""
    return ([m for m, key in (("local", "password_hash"), ("microsoft", "sso_microsoft"), ("google", "sso_google")) if u.get(key)])


def public(u: dict) -> dict:
    """The user without anything sensitive (and whether a password has been chosen yet)."""
    out = {k: v for k, v in u.items() if k not in _PRIVATE}
    out["credentials_set"] = bool(u.get("password_hash"))
    out["sign_in"] = sign_in_methods(u)
    return out


def default_department_id(db: Database) -> int:
    with db.session() as s:
        d = s.scalar(select(Department.id).where(Department.is_default.is_(True)))
        if d is None:
            raise ValueError("There is no default department.")
        return d


def _active_admins(s) -> int:
    return s.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.status == "Active")) or 0


# ---- create ---------------------------------------------------------------------------------------
def create_user(db: Database, email: str, name: str, role: str, department_id: int | None, *,
                domain: str = "", provider: str = "local", password: str | None = None) -> dict:
    """A new person. Admins and Editors get a password through a one-time link (or `password` for the CLI)."""
    e = validate_email(email, domain)
    if role not in ROLES:
        raise ValueError("Role must be Admin, Editor or Viewer.")
    name = (name or "").strip() or e.split("@")[0]
    if password is not None:
        validate_password(password)
    with db.session() as s:
        if s.scalar(select(User.id).where(User.email == e)):
            raise ValueError("That person is already in the list.")
        dept = department_id if department_id is not None else s.scalar(select(Department.id).where(Department.is_default.is_(True)))
        if s.get(Department, dept) is None:
            raise ValueError("That department does not exist.")
        u = User(email=e, name=name[:120], role=role, department_id=dept, status="Active", provider=provider,
                 password_hash=hash_password(password) if password else None)
        s.add(u)
        s.flush()
        return _view(u)


def create_admin(db: Database, email: str, password: str, *, domain: str = "", provider: str = "local",
                 role: str = "admin") -> dict:
    """First admin from the command line (and the legacy /admins endpoint): active, default department."""
    return create_user(db, email, "", role, None, domain=domain, provider=provider, password=password)


# ---- read ------------------------------------------------------------------------------------------
def get_user(db: Database, email: str) -> dict | None:
    with db.session() as s:
        u = s.scalar(select(User).where(User.email == normalize_email(email)))
        return _view(u) if u else None


get_admin = get_user          # older name, still used by the sign-in code


def get_user_by_id(db: Database, uid: int) -> dict | None:
    with db.session() as s:
        u = s.get(User, uid)
        return _view(u) if u else None


def list_users(db: Database) -> list[dict]:
    with db.session() as s:
        names = dict(s.execute(select(Department.id, Department.name)).all())
        out = []
        for u in s.scalars(select(User).order_by(User.email)):
            row = public(_view(u))
            row["department"] = names.get(u.department_id, "")
            out.append(row)
        return out


def list_admins(db: Database) -> list[dict]:
    return [u for u in list_users(db) if u["role"] == "admin"]


def count_admins(db: Database) -> int:
    with db.session() as s:
        return s.scalar(select(func.count()).select_from(User).where(User.role == "admin")) or 0


# ---- change ----------------------------------------------------------------------------------------
def update_user(db: Database, uid: int, *, name: str | None = None, role: str | None = None,
                department_id: int | None = None, status: str | None = None) -> dict:
    with db.session() as s:
        u = s.get(User, uid)
        if u is None:
            raise ValueError("No such person.")
        new_role = role if role is not None else u.role
        new_status = status if status is not None else u.status
        if new_role not in ROLES:
            raise ValueError("Role must be Admin, Editor or Viewer.")
        if new_status not in STATUSES:
            raise ValueError("Status must be Active or Deactivated.")
        if department_id is not None and s.get(Department, department_id) is None:
            raise ValueError("That department does not exist.")
        was_active_admin = u.role == "admin" and u.status == "Active"
        will_be_active_admin = new_role == "admin" and new_status == "Active"
        if was_active_admin and not will_be_active_admin and _active_admins(s) <= 1:
            raise ValueError("You cannot demote or deactivate the last remaining Admin.")
        if name is not None:
            u.name = name.strip()[:120] or u.name
        u.role, u.status = new_role, new_status
        if department_id is not None:
            u.department_id = department_id
        if new_role != "editor":
            s.execute(delete(DepartmentEditor).where(DepartmentEditor.user_id == uid))
        s.flush()
        return _view(u)


def delete_admin(db: Database, email: str) -> bool:
    """Remove a person. The last remaining Admin cannot be removed."""
    e = normalize_email(email)
    with db.session() as s:
        u = s.scalar(select(User).where(User.email == e))
        if u is None:
            return False
        if u.role == "admin" and u.status == "Active" and _active_admins(s) <= 1:
            raise ValueError("You cannot remove the last admin.")
        s.execute(delete(DepartmentEditor).where(DepartmentEditor.user_id == u.id))
        s.delete(u)
        return True


def set_password(db: Database, email: str, new_password: str) -> None:
    validate_password(new_password)
    with db.session() as s:
        u = s.scalar(select(User).where(User.email == normalize_email(email)))
        if u is None:
            raise ValueError("No such admin.")
        u.password_hash = hash_password(new_password)


# ---- sign-in ----------------------------------------------------------------------------------------
def authenticate(db: Database, email: str, password: str) -> dict | None:
    """Only an Active Admin or Editor with a local password can sign in to the panel."""
    u = get_user(db, email)
    if (u is None or u["provider"] != "local" or not u["password_hash"] or u["status"] != "Active"
            or u["role"] not in PANEL_ROLES):
        dummy_verify(password or "")
        return None
    return u if verify_password(password or "", u["password_hash"]) else None


def authenticate_chat(db: Database, email: str, password: str) -> dict | None:
    """Web UI sign-in: any Active person with a local password (Viewers included)."""
    u = get_user(db, email)
    if u is None or u["provider"] != "local" or not u["password_hash"] or u["status"] != "Active":
        dummy_verify(password or "")
        return None
    return u if verify_password(password or "", u["password_hash"]) else None


def sso_sign_in(db: Database, provider: str, subject: str, email: str) -> tuple[dict | None, str]:
    """The panel user for a verified Microsoft or Google account, as (user, outcome).

    Nobody is created here: the person must already be in the list. The first sign-in links the account
    to the person with the same email; after that the link is used, so a later change of email in
    Microsoft or Google cannot move the sign-in to someone else. Outcomes: "ok", "linked" (first sign-in),
    "not_invited", "not_allowed" (deactivated or a Viewer) and "linked_elsewhere" (the email's person is
    already linked to a different account).
    """
    column = {"microsoft": User.sso_microsoft, "google": User.sso_google}[provider]
    attr = "sso_" + provider
    with db.session() as s:
        u = s.scalar(select(User).where(column == subject))
        outcome = "ok"
        if u is None:
            u = s.scalar(select(User).where(User.email == normalize_email(email)))
            if u is None:
                return None, "not_invited"
            if getattr(u, attr):
                return None, "linked_elsewhere"
            outcome = "linked"
        if u.status != "Active" or u.role not in PANEL_ROLES:
            return None, "not_allowed"
        if outcome == "linked":
            setattr(u, attr, subject)
        s.flush()
        return _view(u), outcome


def unlink_sso(db: Database, uid: int, provider: str) -> dict:
    """Remove a linked Microsoft or Google account and end the person's sessions."""
    if provider not in SSO_PROVIDERS:
        raise ValueError("Unknown sign-in provider.")
    with db.session() as s:
        u = s.get(User, uid)
        if u is None:
            raise ValueError("No such person.")
        if not getattr(u, "sso_" + provider):
            raise ValueError("That account is not linked.")
        setattr(u, "sso_" + provider, None)
        u.session_version = (u.session_version or 0) + 1
        s.flush()
        return _view(u)


def touch_sign_in(db: Database, email: str) -> None:
    with db.session() as s:
        u = s.scalar(select(User).where(User.email == normalize_email(email)))
        if u is not None:
            u.last_sign_in = _now()


# ---- one-time set-password links ----------------------------------------------------------------------
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_set_password_link(db: Database, uid: int, created_by: str) -> tuple[str, datetime]:
    """A fresh single-use token (valid LINK_HOURS). Any earlier unused link for the person is cancelled.
    Only a hash is stored: the plain token is returned once and cannot be looked up again."""
    token = secrets.token_urlsafe(32)
    expires = _now() + timedelta(hours=LINK_HOURS)
    with db.session() as s:
        u = s.get(User, uid)
        if u is None:
            raise ValueError("No such person.")
        if u.status != "Active":
            raise ValueError("Activate this person first.")
        s.execute(delete(SetPasswordToken).where(SetPasswordToken.user_id == uid, SetPasswordToken.used_at.is_(None)))
        s.add(SetPasswordToken(user_id=uid, token_hash=_hash_token(token), created_by=created_by, expires_at=expires))
    return token, expires


def redeem_set_password_link(db: Database, token: str, new_password: str) -> dict:
    """Choose a password with a link. Raises ValueError for an unknown, used or expired link."""
    validate_password(new_password)
    with db.session() as s:
        row = s.scalar(select(SetPasswordToken).where(SetPasswordToken.token_hash == _hash_token(token or "")))
        expires = row.expires_at.replace(tzinfo=timezone.utc) if row is not None and row.expires_at.tzinfo is None \
            else (row.expires_at if row is not None else None)
        if row is None or row.used_at is not None or expires <= _now():
            raise ValueError("This link is not valid any more. Ask an Admin for a new one.")
        u = s.get(User, row.user_id)
        if u is None or u.status != "Active":
            raise ValueError("This link is not valid any more. Ask an Admin for a new one.")
        u.password_hash = hash_password(new_password)
        row.used_at = _now()
        s.flush()
        return _view(u)
