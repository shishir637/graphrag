"""Secrets of outside sources (GitHub tokens, database connection strings, webhook secrets), encrypted with Fernet
(AES-128-CBC + HMAC) before they are stored (table `secrets`). Values are never logged or returned by the API,
only a hint (first and last characters).

The key: CL_SECRET_KEY if set, else a key generated once into DATA_DIR/secret.key (fine on one machine).
"""
import os
import threading

from . import core
from . import db as D

_lock = threading.Lock()
_fernet = None

SCHEMA = """CREATE TABLE IF NOT EXISTS secrets (name TEXT NOT NULL, ciphertext TEXT NOT NULL,
  hint TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL{tenant}, PRIMARY KEY ({tk}name))"""
_ready: str | None = None


def _db():
    return core.db_with(SCHEMA)


def _keys() -> list[bytes]:
    k = os.environ.get("CL_SECRET_KEY", "").strip()
    if k:
        return [x.strip().encode() for x in k.replace(",", "\n").splitlines() if x.strip()]
    from cryptography.fernet import Fernet
    path = core.DATA_DIR / "secret.key"
    with _lock:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(Fernet.generate_key())
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        return [path.read_bytes().strip()]


def _f():
    global _fernet
    if _fernet is None:
        from cryptography.fernet import Fernet, MultiFernet
        _fernet = MultiFernet([Fernet(k) for k in _keys()])
    return _fernet


def reset_for_tests():
    global _fernet, _ready
    _fernet, _ready = None, None


def hint(value: str) -> str:
    return core.mask(value) or ""


def put(name: str, value: str | None):
    """Store (or with an empty value, remove) a secret."""
    if not value:
        _db().execute("DELETE FROM secrets WHERE name = ?", (name,))
        return
    _db().execute("INSERT INTO secrets (name, ciphertext, hint, updated) VALUES (?, ?, ?, ?) "
                  f"ON CONFLICT {D.ck('name')} DO UPDATE SET ciphertext = excluded.ciphertext, hint = excluded.hint, "
                  "updated = excluded.updated", (name, _f().encrypt(value.encode()).decode(), hint(value), core.now()))


def get(name: str) -> str | None:
    r = _db().execute("SELECT ciphertext FROM secrets WHERE name = ?", (name,)).fetchone()
    return _f().decrypt(r["ciphertext"].encode()).decode() if r else None


def get_hint(name: str) -> str | None:
    r = _db().execute("SELECT hint FROM secrets WHERE name = ?", (name,)).fetchone()
    return r["hint"] if r else None
