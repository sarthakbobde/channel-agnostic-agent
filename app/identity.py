"""Who is asking? Maps the sender of a chat message to a person in the Users list.

Adapters put the sender's email on the Envelope (looked up from the platform where needed). Here the
email becomes an Identity:
  * a known, Active person       -> their own role and department;
  * an email we do not have      -> the default department, role Viewer, `known=False` (D13);
  * a Deactivated person         -> `active=False`: the assistant politely refuses.
A problem with the lookup must never stop an answer, so any failure falls back to "unknown".
"""
import contextvars
import logging
import threading
import time
from dataclasses import dataclass

from sqlalchemy import func, select

from app import alerts
from app.db import Database, Department, DepartmentEditor, GroupMember, User, get_db

log = logging.getLogger(__name__)
DENIED_REPLY = "Your access to this assistant has been turned off. Please contact an administrator."
HIT_SECONDS, MISS_SECONDS = 600, 120


@dataclass(frozen=True)
class Identity:
    email: str = ""
    name: str = ""
    role: str = "viewer"
    department_id: int | None = None
    department: str = ""
    known: bool = False
    active: bool = True
    user_id: int | None = None
    group_ids: tuple = ()            # groups the person belongs to RIGHT NOW (read when the question is asked)
    edits_departments: tuple = ()    # departments this person is an Editor of


def resolve(email: str, db: Database | None = None) -> Identity:
    email = (email or "").strip().lower()
    try:
        db = db or get_db()
        with db.session() as s:
            u = s.scalar(select(User).where(func.lower(User.email) == email)) if email else None
            if u is not None:
                dept = s.get(Department, u.department_id)
                groups = tuple(sorted(s.scalars(select(GroupMember.group_id).where(GroupMember.user_id == u.id))))
                edits = tuple(sorted(s.scalars(select(DepartmentEditor.department_id).where(DepartmentEditor.user_id == u.id)))) \
                    if u.role == "editor" else ()
                return Identity(u.email, u.name, u.role, u.department_id, dept.name if dept else "",
                                known=True, active=u.status == "Active", user_id=u.id, group_ids=groups, edits_departments=edits)
            d = s.scalar(select(Department).where(Department.is_default.is_(True)))
            return Identity(email, "", "viewer", d.id if d else None, d.name if d else "", known=False)
    except Exception:
        log.warning("could not resolve sender identity; treating as unknown", exc_info=True)
        return Identity(email)


# ---- department of the turn being answered (read by the usage recorder) -----------------
_department: contextvars.ContextVar[str | None] = contextvars.ContextVar("department", default=None)


_who: contextvars.ContextVar[Identity | None] = contextvars.ContextVar("who", default=None)


def bind(identity: Identity):
    return (_department.set(identity.department or None), _who.set(identity))


def unbind(token) -> None:
    _department.reset(token[0])
    _who.reset(token[1])


def current() -> Identity:
    """Who is asking the question being answered. Nobody bound (a script, a debugging call) counts as an
    unknown Viewer, which can only see documents visible to Everyone: the safe default."""
    return _who.get() or Identity()


def current_department() -> str | None:
    return _department.get()


# ---- small cache for platform lookups (user id -> email) --------------------------------
class TTLCache:
    def __init__(self, hit: int = HIT_SECONDS, miss: int = MISS_SECONDS):
        self.hit, self.miss, self._d, self._lock = hit, miss, {}, threading.Lock()

    def get(self, key):
        with self._lock:
            row = self._d.get(key)
        if row and row[1] > time.monotonic():
            return True, row[0]
        return False, None

    def put(self, key, value) -> None:
        ttl = self.hit if value else self.miss
        with self._lock:
            self._d[key] = (value, time.monotonic() + ttl)


def lookup_problem(key: str, message: str) -> None:
    """Tell admins (once, as a banner) that sender emails cannot be read on a channel."""
    try:
        alerts.raise_alert(get_db(), key, "identity_lookup", message, "warning")
    except Exception:
        log.warning("could not raise alert %s", key, exc_info=True)


def lookup_ok(key: str) -> None:
    try:
        alerts.resolve_key(get_db(), key)
    except Exception:
        pass
