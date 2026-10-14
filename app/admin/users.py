"""Admin accounts. Local password sign-in now; `provider` leaves room for Entra ID later."""
import re

from sqlalchemy import func, select

from app.admin.auth import dummy_verify, hash_password, verify_password
from app.db import AdminUser, Database

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 12


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_email(email: str, domain: str = "") -> str:
    e = normalize_email(email)
    if not _EMAIL.match(e):
        raise ValueError("Enter a valid email address.")
    if domain and not e.endswith("@" + domain.lower()):
        raise ValueError(f"Only @{domain} addresses can be admins.")
    return e


def validate_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise ValueError(f"Password must be at least {MIN_PASSWORD} characters.")


def _view(u: AdminUser) -> dict:
    return {"email": u.email, "role": u.role, "provider": u.provider, "created_at": u.created_at,
            "password_hash": u.password_hash}


def public(u: dict) -> dict:
    """The user without anything sensitive."""
    return {k: v for k, v in u.items() if k != "password_hash"}


def create_admin(db: Database, email: str, password: str, *, domain: str = "",
                 provider: str = "local", role: str = "admin") -> dict:
    e = validate_email(email, domain)
    validate_password(password)
    with db.session() as s:
        if s.scalar(select(AdminUser).where(AdminUser.email == e)):
            raise ValueError("That person is already an admin.")
        u = AdminUser(email=e, role=role, provider=provider, password_hash=hash_password(password))
        s.add(u)
        s.flush()
        return _view(u)


def get_admin(db: Database, email: str) -> dict | None:
    with db.session() as s:
        u = s.scalar(select(AdminUser).where(AdminUser.email == normalize_email(email)))
        return _view(u) if u else None


def authenticate(db: Database, email: str, password: str) -> dict | None:
    u = get_admin(db, email)
    if u is None or u["provider"] != "local" or not u["password_hash"]:
        dummy_verify(password or "")
        return None
    return u if verify_password(password or "", u["password_hash"]) else None


def list_admins(db: Database) -> list[dict]:
    with db.session() as s:
        return [public(_view(u)) for u in s.scalars(select(AdminUser).order_by(AdminUser.email))]


def count_admins(db: Database) -> int:
    with db.session() as s:
        return s.scalar(select(func.count()).select_from(AdminUser)) or 0


def delete_admin(db: Database, email: str) -> bool:
    e = normalize_email(email)
    with db.session() as s:
        u = s.scalar(select(AdminUser).where(AdminUser.email == e))
        if u is None:
            return False
        if (s.scalar(select(func.count()).select_from(AdminUser)) or 0) <= 1:
            raise ValueError("You cannot remove the last admin.")
        s.delete(u)
        return True


def set_password(db: Database, email: str, new_password: str) -> None:
    validate_password(new_password)
    with db.session() as s:
        u = s.scalar(select(AdminUser).where(AdminUser.email == normalize_email(email)))
        if u is None:
            raise ValueError("No such admin.")
        u.password_hash = hash_password(new_password)
