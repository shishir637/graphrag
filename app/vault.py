"""A workspace's secrets (connector tokens, database passwords, the AI extraction key), encrypted with Fernet
(AES-128-CBC + HMAC) before they are stored in Postgres (table secrets). The key is CL_SECRET_KEY; without it a key
is generated once into DATA_DIR/secret.key (fine for one machine; set CL_SECRET_KEY in production and keep it safe:
without it the stored secrets cannot be read). Values are never logged or returned by the API, only a hint.
"""
import os
import threading

from . import core

_lock = threading.Lock()
_fernet = None


def _key() -> bytes:
    k = os.environ.get("CL_SECRET_KEY", "").strip()
    if k:
        return k.encode()
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
        return path.read_bytes().strip()


def _f():
    global _fernet
    if _fernet is None:
        from cryptography.fernet import Fernet
        _fernet = Fernet(_key())
    return _fernet


def reset_for_tests():
    global _fernet
    _fernet = None


def hint(value: str) -> str:
    return core.mask(value) or ""


def put(name: str, value: str | None):
    """Store (or with an empty value, remove) a secret of the current workspace."""
    t = core.current_tenant()
    if not t:
        raise RuntimeError("No workspace in scope.")
    if not value:
        core.db().execute("DELETE FROM secrets WHERE tenant_id = ? AND name = ?", (t, name))
        return
    core.db().execute("INSERT INTO secrets (tenant_id, name, ciphertext, hint, updated) VALUES (?, ?, ?, ?, ?) "
                      "ON CONFLICT (tenant_id, name) DO UPDATE SET ciphertext = excluded.ciphertext, hint = excluded.hint, "
                      "updated = excluded.updated",
                      (t, name, _f().encrypt(value.encode()).decode(), hint(value), core.now()))


def get(name: str) -> str | None:
    t = core.current_tenant()
    if not t:
        return None
    r = core.db().execute("SELECT ciphertext FROM secrets WHERE tenant_id = ? AND name = ?", (t, name)).fetchone()
    return _f().decrypt(r["ciphertext"].encode()).decode() if r else None


def get_hint(name: str) -> str | None:
    t = core.current_tenant()
    r = core.db().execute("SELECT hint FROM secrets WHERE tenant_id = ? AND name = ?", (t, name)).fetchone() if t else None
    return r["hint"] if r else None


def value(name: str) -> str:
    """For engines: the workspace's secret, else the server's environment variable of the same name."""
    try:
        return get(name) or os.environ.get(name, "")
    except Exception:                                      # noqa: BLE001 - outside the app / no database
        return os.environ.get(name, "")
