"""Encryption for credentials stored in the admin database.

SecretStore is the seam: today the master key comes from the APP_SECRET_KEY environment
variable (Fernet, AES-128-CBC + HMAC). A cloud key vault can replace FernetSecretStore
later without touching the rest of the code.

APP_SECRET_KEY may hold several comma-separated keys. The first encrypts new values; all of
them can decrypt, which allows key rotation (add a new key in front, run rotate, remove the
old key).

    python -m app.secretstore generate     # print a new key
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


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["generate"]:
        print(generate_key())
    else:
        print(__doc__)
