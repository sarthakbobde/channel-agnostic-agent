"""Where uploaded originals (and any other stored file) live: always encrypted, on local disk or in Azure Blob Storage.

Every file is encrypted with the master key (Fernet, the same key as the stored credentials, so `secretstore rotate`
covers files too) before it is written, whatever its type. A backend only ever sees ciphertext: someone who copies the
folder, a backup or the blob container gets nothing readable, and the blob provider never holds readable content.

Keys are relative paths such as "3/41/HR-Policy.pdf" (<kb id>/<document id>/<file name>).

Parsers need a real file for PDF and Office documents, so `plain_copy()` decrypts into a private working folder
(`<DOCS_DIR>/.work`, owner-only) and deletes it as soon as the block ends. Anything a crash left there is wiped at start.

    python -m app.knowledge.filestore status          # how many stored files are still unencrypted
    python -m app.knowledge.filestore encrypt         # encrypt them in place (repeatable, resumes after an interruption)
    python -m app.knowledge.filestore secure <folder>  # encrypt every file in another folder in place (e.g. an old chats backup)
"""
import contextlib
import logging
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator

from app.secretstore import FernetSecretStore, SecretError

log = logging.getLogger(__name__)
MAGIC = b"CAAENC1\n"                  # marks an encrypted file; anything without it is a plain file from before 12a
WORK = ".work"


# ---- backends: raw bytes only, no knowledge of encryption ------------------------------------------------------------
class Backend(ABC):
    @abstractmethod
    def read(self, key: str) -> bytes: ...

    @abstractmethod
    def write(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def keys(self, prefix: str = "") -> Iterator[str]: ...

    def delete_prefix(self, prefix: str) -> None:
        for k in list(self.keys(prefix)):
            self.delete(k)


class LocalBackend(Backend):
    def __init__(self, root: str):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if not p.is_relative_to(self.root.resolve()) or key.startswith(WORK):
            raise ValueError("Invalid file key.")
        return p

    def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def write(self, key: str, data: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".partial")
        tmp.write_bytes(data)
        os.replace(tmp, p)                  # a crash never leaves half a file under the real name

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        p = self._path(key)
        p.unlink(missing_ok=True)
        for parent in (p.parent, p.parent.parent):          # drop empty <doc> and <kb> folders
            if parent != self.root.resolve() and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()

    def keys(self, prefix: str = "") -> Iterator[str]:
        if not self.root.is_dir():
            return
        root = self.root.resolve()
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root).as_posix()
            if p.is_file() and not rel.startswith(WORK) and not rel.endswith(".partial") and rel.startswith(prefix):
                yield rel

    def delete_prefix(self, prefix: str) -> None:
        target = self._path(prefix.rstrip("/"))
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        else:
            super().delete_prefix(prefix)


class AzureBlobBackend(Backend):
    """Azure Blob Storage. Holds the same encrypted bytes as the local folder would."""

    def __init__(self, connection_string: str, container: str):
        try:
            from azure.storage.blob import BlobServiceClient
        except ImportError:
            raise RuntimeError("FILE_STORE=azure_blob needs the azure-storage-blob package: pip install -e .[azure-blob]") from None
        if not connection_string:
            raise RuntimeError("FILE_STORE=azure_blob needs AZURE_BLOB_CONNECTION_STRING.")
        self._container = BlobServiceClient.from_connection_string(connection_string).get_container_client(container)
        if not self._container.exists():
            self._container.create_container()

    def read(self, key: str) -> bytes:
        return self._container.download_blob(key).readall()

    def write(self, key: str, data: bytes) -> None:
        self._container.upload_blob(key, data, overwrite=True)

    def exists(self, key: str) -> bool:
        return self._container.get_blob_client(key).exists()

    def delete(self, key: str) -> None:
        from azure.core.exceptions import ResourceNotFoundError
        try:
            self._container.delete_blob(key)
        except ResourceNotFoundError:
            pass

    def keys(self, prefix: str = "") -> Iterator[str]:
        for b in self._container.list_blobs(name_starts_with=prefix or None):
            yield b.name


# ---- the store: encrypts on the way in, decrypts on the way out ------------------------------------------------------
class FileStore:
    def __init__(self, backend: Backend, keys: str, work_root: str):
        self.backend = backend
        self._keys = keys
        self.work = Path(work_root) / WORK

    def _crypto(self) -> FernetSecretStore:
        try:
            return FernetSecretStore(self._keys)
        except SecretError as e:
            raise ValueError("Files cannot be stored: " + str(e)) from None

    def put(self, key: str, data: bytes) -> None:
        self.backend.write(key, MAGIC + self._crypto().encrypt_bytes(data))

    def get(self, key: str) -> bytes:
        raw = self.backend.read(key)
        if not raw.startswith(MAGIC):
            log.warning("stored file %s is not encrypted yet; run: python -m app.knowledge.filestore encrypt", key)
            return raw
        return self._crypto().decrypt_bytes(raw[len(MAGIC):])

    def exists(self, key: str) -> bool:
        return self.backend.exists(key)

    def delete(self, key: str) -> None:
        self.backend.delete(key)

    def delete_prefix(self, prefix: str) -> None:
        self.backend.delete_prefix(prefix)

    def keys(self, prefix: str = "") -> list[str]:
        return list(self.backend.keys(prefix))

    def move_prefix(self, old: str, new: str) -> None:
        """Move every file under `old` to `new` (ciphertext is copied as it is, nothing is decrypted)."""
        for k in self.keys(old):
            self.backend.write(new + k[len(old):], self.backend.read(k))
            self.backend.delete(k)

    # -- plain text on disk, only for as long as a parser needs it --
    @contextlib.contextmanager
    def plain_copy(self, key: str, name: str | None = None) -> Iterator[Path]:
        with self.plain_file(self.get(key), name or Path(key).name) as p:
            yield p

    @contextlib.contextmanager
    def plain_file(self, data: bytes, name: str) -> Iterator[Path]:
        """A private, short-lived file holding `data` (also used for upload previews, which are never stored)."""
        self.work.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(dir=self.work))          # created with owner-only permissions
        try:
            path = folder / Path(name).name
            path.write_bytes(data)
            yield path
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def wipe_work(self) -> int:
        """Remove anything a crash left in the working folder. Returns how many files were removed."""
        if not self.work.is_dir():
            return 0
        n = sum(1 for p in self.work.rglob("*") if p.is_file())
        shutil.rmtree(self.work, ignore_errors=True)
        return n

    # -- migration and rotation --
    def plain_keys(self) -> list[str]:
        return [k for k in self.keys() if not self.backend.read(k).startswith(MAGIC)]

    def encrypt_plain(self) -> int:
        """Encrypt every file written before encryption existed. Each file is replaced in one step, so an interrupted run
        just resumes. Returns how many files were encrypted."""
        crypto, n = self._crypto(), 0
        for k in self.keys():
            raw = self.backend.read(k)
            if not raw.startswith(MAGIC):
                self.backend.write(k, MAGIC + crypto.encrypt_bytes(raw))
                n += 1
        return n

    def rotate(self) -> dict:
        """Re-encrypt every file with the first key. Checks every file first, so nothing changes if one cannot be read."""
        crypto = self._crypto()
        keys = self.keys()
        for k in keys:
            raw = self.backend.read(k)
            if raw.startswith(MAGIC):
                crypto.decrypt_bytes(raw[len(MAGIC):])           # raises SecretError before anything is written
        rotated = 0
        for k in keys:
            raw = self.backend.read(k)
            if raw.startswith(MAGIC):
                new = MAGIC + crypto.rotate_bytes(raw[len(MAGIC):])
                if new != raw:
                    self.backend.write(k, new)
                    rotated += 1
        return {"total": len(keys), "rotated": rotated}


def build(settings) -> FileStore:
    kind = (getattr(settings, "file_store", "") or "local").lower()
    if kind == "local":
        backend: Backend = LocalBackend(settings.docs_dir)
    elif kind == "azure_blob":
        backend = AzureBlobBackend(settings.azure_blob_connection_string, settings.azure_blob_container)
    else:
        raise RuntimeError(f"Unknown FILE_STORE '{kind}'. Use local or azure_blob.")
    return FileStore(backend, settings.secret_key, settings.docs_dir)


# ---- files outside the store (legacy folders) --------------------------------------------------------------------------
def secure_folder(folder: str, keys: str) -> int:
    """Encrypt every plain file in a folder in place (same marker as stored files). Returns how many were encrypted."""
    crypto = FernetSecretStore(keys)
    n = 0
    for p in sorted(Path(folder).rglob("*")):
        if p.is_file():
            raw = p.read_bytes()
            if not raw.startswith(MAGIC):
                tmp = p.with_name(p.name + ".partial")
                tmp.write_bytes(MAGIC + crypto.encrypt_bytes(raw))
                os.replace(tmp, p)
                n += 1
    return n


def read_maybe_encrypted(path: Path, keys: str) -> bytes:
    """A file from a folder that may have been secured with `secure_folder`."""
    raw = path.read_bytes()
    if raw.startswith(MAGIC):
        return FernetSecretStore(keys).decrypt_bytes(raw[len(MAGIC):])
    return raw


def _cli(argv: list[str]) -> int:
    from app.config import settings
    cmd = argv[0] if argv else ""
    try:
        if cmd == "status":
            store = build(settings)
            plain = store.plain_keys()
            print(f"{len(store.keys())} stored files; {len(plain)} not encrypted yet.")
            return 1 if plain else 0
        if cmd == "encrypt":
            print(f"{build(settings).encrypt_plain()} files encrypted.")
            return 0
        if cmd == "secure" and len(argv) > 1:
            print(f"{secure_folder(argv[1], settings.secret_key)} files in {argv[1]} encrypted.")
            return 0
    except (SecretError, ValueError, RuntimeError) as e:
        print("Nothing was changed. " + str(e))
        return 1
    print(__doc__)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli(sys.argv[1:]))
