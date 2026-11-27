"""A workspace's secrets (connector tokens, database passwords, the AI extraction key), encrypted with Fernet
(AES-128-CBC + HMAC) before they are stored in Postgres (table secrets). Values are never logged or returned by
the API, only a hint.

The master key comes from the first of these that gives one (several keys, newest first, allow rotation):
  1. CL_SECRET_KEY            one key, or several separated by commas (newest first)
  2. CL_SECRET_KEY_FILE       a file with one key per line, newest first (Docker / Kubernetes secrets)
  3. CL_SECRET_KEY_PROVIDER   a secrets manager (its client library is an optional dependency):
                                aws:<secret id>                       AWS Secrets Manager (boto3)
                                gcp:projects/<p>/secrets/<s>/versions/latest   GCP Secret Manager
                                azure:https://<vault>.vault.azure.net/<secret name>   Azure Key Vault
                                vault:<mount>/<path>#<field>          OpenBao / HashiCorp Vault KV v2 (hvac;
                                                                      VAULT_ADDR and VAULT_TOKEN as usual)
                                module:<package.module>:<function>    your own: returns the key(s) as a string
Without any of them a key is generated once into DATA_DIR/secret.key: fine for development on one machine.
With CL_ENV=production the app and the worker refuse to start instead (check_startup), so a production install
never depends on a key file it might lose.

Rotation: put the new key first (keep the old ones after it), restart, then re-encrypt every stored secret with the
newest key: python -m app.vault rotate. Old keys can be removed once that reports the rows it rewrote.
"""
import os
import sys
import threading

from . import core

_lock = threading.Lock()
_fernet = None


def _split(text: str) -> list[bytes]:
    return [k.strip().encode() for k in text.replace(",", "\n").splitlines() if k.strip()]


def _provider(spec: str) -> str:
    kind, _, ref = spec.partition(":")
    if kind == "aws":
        import boto3
        return boto3.client("secretsmanager").get_secret_value(SecretId=ref)["SecretString"]
    if kind == "gcp":
        from google.cloud import secretmanager
        return secretmanager.SecretManagerServiceClient().access_secret_version(name=ref).payload.data.decode()
    if kind == "azure":
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
        vault_url, _, name = ref.rstrip("/").rpartition("/")
        return SecretClient(vault_url=vault_url, credential=DefaultAzureCredential()).get_secret(name).value
    if kind == "vault":
        import hvac
        path, _, field = ref.partition("#")
        mount, _, sub = path.partition("/")
        c = hvac.Client(url=os.environ.get("VAULT_ADDR"), token=os.environ.get("VAULT_TOKEN"))
        return c.secrets.kv.v2.read_secret_version(path=sub, mount_point=mount)["data"]["data"][field or "key"]
    if kind == "module":
        import importlib
        mod, _, fn = ref.partition(":")
        return getattr(importlib.import_module(mod), fn)()
    raise ValueError(f"CL_SECRET_KEY_PROVIDER={spec}: use aws:, gcp:, azure:, vault: or module:.")


def configured_keys() -> list[bytes]:
    """The master keys from the environment, a key file or a secrets manager (newest first); [] when none is set."""
    from .envfile import env
    k = os.environ.get("CL_SECRET_KEY", "").strip()
    if k:
        return _split(k)
    if os.environ.get("CL_SECRET_KEY_FILE"):
        return _split(env("CL_SECRET_KEY"))
    spec = os.environ.get("CL_SECRET_KEY_PROVIDER", "").strip()
    if spec:
        return _split(_provider(spec))
    return []


def production() -> bool:
    return os.environ.get("CL_ENV", "").strip().lower() in ("production", "prod")


def check_startup():
    """Called when the app or the worker starts: in production a master key must be configured."""
    if production() and not configured_keys():
        raise SystemExit("CL_ENV=production: set CL_SECRET_KEY, CL_SECRET_KEY_FILE or CL_SECRET_KEY_PROVIDER "
                         "(the key that encrypts stored secrets); refusing to start with a generated key file.")


def _keys() -> list[bytes]:
    keys = configured_keys()
    if keys:
        return keys
    if production():
        check_startup()
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


def rotate() -> int:
    """Re-encrypt every stored secret (all workspaces) with the newest master key. Returns the rows rewritten."""
    f = _f()
    n = 0
    from .db import tx
    with core.system(), tx() as c:
        for r in c.execute("SELECT tenant_id, name, ciphertext FROM secrets").fetchall():
            new = f.rotate(r["ciphertext"].encode()).decode()
            if new != r["ciphertext"]:
                c.execute("UPDATE secrets SET ciphertext = ?, updated = ? WHERE tenant_id = ? AND name = ?",
                          (new, core.now(), r["tenant_id"], r["name"]))
                n += 1
    return n


if __name__ == "__main__":
    if sys.argv[1:] != ["rotate"]:
        raise SystemExit("usage: python -m app.vault rotate   (re-encrypt every stored secret with the newest key)")
    print(f"re-encrypted {rotate()} secrets with the newest key")
