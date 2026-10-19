"""Encryption for credentials stored in the admin database.

SecretStore is the seam: today the master key comes from the APP_SECRET_KEY environment
variable (Fernet, AES-128-CBC + HMAC). A cloud key vault can replace FernetSecretStore
later without touching the rest of the code.

APP_SECRET_KEY may hold several comma-separated keys. The first encrypts new values; all of
them can decrypt, which allows key rotation (add a new key in front, run rotate, remove the
old key).

    python -m app.secretstore generate     # print a new key
    python -m app.secretstore fingerprint  # a short, non-secret tag of each configured key (to check a backup copy)
    python -m app.secretstore check        # can every stored secret be decrypted with the configured keys?
    python -m app.secretstore rotate       # re-encrypt every stored secret with the first key (all or nothing)
"""
from abc import ABC, abstractmethod
from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class SecretError(Exception):
    """Missing or wrong master key, or undecryptable data."""


class SecretStore(ABC):
    @abstractmethod
    def encrypt(self, plaintext: str) -> str: ...

    @abstractmethod
    def decrypt(self, token: str) -> str: ...


class FernetSecretStore(SecretStore):
    def __init__(self, keys: str):
        parts = [k.strip() for k in (keys or "").split(",") if k.strip()]
        if not parts:
            raise SecretError("APP_SECRET_KEY is not set. Generate one with: "
                              "python -m app.secretstore generate")
        try:
            self._fernet = MultiFernet([Fernet(k.encode()) for k in parts])
        except (ValueError, TypeError) as e:
            raise SecretError(f"APP_SECRET_KEY is not a valid key: {e}") from None

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken:
            raise SecretError("Cannot decrypt a stored secret: the master key is wrong or "
                              "has changed.") from None

    def rotate(self, token: str) -> str:
        """Re-encrypt a stored token with the primary (first) key."""
        try:
            return self._fernet.rotate(token.encode("ascii")).decode("ascii")
        except InvalidToken:
            raise SecretError("Cannot rotate: the token is not decryptable with the "
                              "configured keys.") from None


def default_store() -> "FernetSecretStore | None":
    """The store built from APP_SECRET_KEY, or None when no (valid) key is configured."""
    from app.config import settings
    try:
        return FernetSecretStore(settings.secret_key)
    except SecretError:
        return None


def fingerprint(key: str) -> str:
    """A short tag that identifies a key without revealing it: compare it with the one noted next to your backup."""
    import hashlib
    return hashlib.sha256(b"caa-key-fingerprint:" + key.strip().encode()).hexdigest()[:10]


def check_all(db, store: "FernetSecretStore") -> dict:
    """Try to decrypt every stored secret. Returns {total, undecryptable: [ids], old_key: n} where old_key counts
    secrets that still use a key other than the first (so rotation would change them)."""
    from sqlalchemy import select
    from app.db import Secret
    bad, old, total = [], 0, 0
    with db.session() as s:
        for row in s.scalars(select(Secret)):
            total += 1
            try:
                store.decrypt(row.ciphertext)
            except SecretError:
                bad.append(row.id)
                continue
            if not _uses_first_key(store, row.ciphertext):
                old += 1
    return {"total": total, "undecryptable": bad, "old_key": old}


def _uses_first_key(store: "FernetSecretStore", token: str) -> bool:
    try:
        store._fernet._fernets[0].decrypt(token.encode("ascii"))
        return True
    except InvalidToken:
        return False


def rotate_all(db, store: "FernetSecretStore") -> dict:
    """Re-encrypt every stored secret with the first key. All or nothing: if any secret cannot be decrypted with the
    configured keys, nothing is changed. Returns {total, rotated}."""
    from sqlalchemy import select
    from app.db import Secret
    with db.session() as s:
        rows = list(s.scalars(select(Secret)))
        new = {}
        for row in rows:
            new[row.id] = store.rotate(row.ciphertext)          # raises SecretError before anything is written
        rotated = 0
        for row in rows:
            if new[row.id] != row.ciphertext:
                row.ciphertext = new[row.id]
                rotated += 1
    return {"total": len(rows), "rotated": rotated}


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


def _cli(argv: list[str]) -> int:
    from app.config import settings
    cmd = argv[0] if argv else ""
    if cmd == "generate":
        print(generate_key())
        return 0
    if cmd == "fingerprint":
        keys = [k.strip() for k in settings.secret_key.split(",") if k.strip()]
        for i, k in enumerate(keys):
            print(f"key {i + 1}{' (encrypts new values)' if i == 0 else ' (decrypt only)'}: {fingerprint(k)}")
        return 0 if keys else 1
    if cmd in ("check", "rotate"):
        from app.db import get_db
        try:
            store = FernetSecretStore(settings.secret_key)
            db = get_db()
            if cmd == "check":
                r = check_all(db, store)
                print(f"{r['total']} stored secrets; {len(r['undecryptable'])} cannot be decrypted; {r['old_key']} still use an older key.")
                return 1 if r["undecryptable"] else 0
            r = rotate_all(db, store)
            print(f"{r['rotated']} of {r['total']} stored secrets re-encrypted with the first key.")
            return 0
        except SecretError as e:
            print("Nothing was changed. " + str(e))
            return 1
    print(__doc__)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli(sys.argv[1:]))
