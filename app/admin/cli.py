"""Admin command line. Passwords are typed at a hidden prompt, never passed as arguments.

    python -m app.admin.cli create-admin you@sysfore.com
    python -m app.admin.cli set-password you@sysfore.com
    python -m app.admin.cli list
"""
import getpass
import sys

from app.config import settings
from app.db import get_db
from app.admin import users


def _ask_password() -> str:
    first = getpass.getpass("New password (min %d characters): " % users.MIN_PASSWORD)
    if first != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match.")
    return first


def main(argv: list[str]) -> int:
    db = get_db()
    cmd = argv[0] if argv else ""
    if cmd == "create-admin" and len(argv) == 2:
        try:
            u = users.create_admin(db, argv[1], _ask_password(), domain=settings.admin_email_domain)
        except ValueError as e:
            sys.exit(str(e))
        print(f"Created admin {u['email']}.")
    elif cmd == "set-password" and len(argv) == 2:
        try:
            users.set_password(db, argv[1], _ask_password())
        except ValueError as e:
            sys.exit(str(e))
        print("Password updated. Existing sessions are signed out.")
    elif cmd == "list":
        for u in users.list_admins(db):
            print(f"{u['email']}  {u['role']}  {u['provider']}")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
