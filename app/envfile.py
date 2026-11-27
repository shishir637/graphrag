"""Settings that may come from a file: NAME, or else the contents of the file named by NAME_FILE.

Docker and Kubernetes hand secrets to a container as files (/run/secrets/...); `env("CL_DATABASE_URL")` reads
CL_DATABASE_URL if it is set, otherwise CL_DATABASE_URL_FILE's file (trailing whitespace removed). Used for
CL_DATABASE_URL, CL_DB_PASSWORD, QDRANT_API_KEY and CL_SECRET_KEY (app/vault.py).
"""
import os


def env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    if v:
        return v
    path = os.environ.get(f"{name}_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError as e:
            raise RuntimeError(f"{name}_FILE={path}: cannot read it ({e.strerror}).") from None
    return default
