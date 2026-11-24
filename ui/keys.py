"""Access keys for agents that connect over MCP.

One key per agent or app. A key carries the access groups it may read (the same groups documents
are tagged with), so each agent only ever sees what its groups allow. The token is shown once when
the key is created; only its SHA-256 hash is stored, in console_data/keys.json (git-ignored).
Every MCP call is appended to console_data/mcp_calls.jsonl for the console's activity view.
"""
import hashlib
import json
import secrets
import threading
from datetime import datetime, timezone

from rag.config import ROOT

DATA = ROOT / "console_data"
KEYS = DATA / "keys.json"
CALLS = DATA / "mcp_calls.jsonl"
_io = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _load() -> list[dict]:
    return json.loads(KEYS.read_text(encoding="utf-8")) if KEYS.exists() else []


def _save(keys: list[dict]):
    DATA.mkdir(exist_ok=True)
    tmp = KEYS.with_suffix(".tmp")
    tmp.write_text(json.dumps(keys, indent=2), encoding="utf-8")
    tmp.replace(KEYS)


def public(k: dict) -> dict:
    return {x: k.get(x) for x in ("id", "name", "groups", "prefix", "created", "last_used", "calls", "revoked")}


def list_keys() -> list[dict]:
    with _io:
        return [public(k) for k in _load()]


def create(name: str, groups: list[str]) -> tuple[dict, str]:
    name = " ".join(name.split())[:60]
    if not name:
        raise ValueError("Give the key a name, for example the agent or app that will use it.")
    groups = sorted({g.strip() for g in groups if g.strip()}) or ["public"]
    token = "clk_" + secrets.token_urlsafe(24)
    rec = {"id": secrets.token_hex(4), "name": name, "groups": groups, "hash": _hash(token), "prefix": token[:8],
           "created": _now(), "last_used": None, "calls": 0, "revoked": False}
    with _io:
        keys = _load()
        keys.append(rec)
        _save(keys)
    return public(rec), token


def revoke(key_id: str) -> dict:
    with _io:
        keys = _load()
        for k in keys:
            if k["id"] == key_id:
                k["revoked"] = True
                _save(keys)
                return public(k)
    raise KeyError(key_id)


def get(key_id: str) -> dict | None:
    with _io:
        return next((public(k) for k in _load() if k["id"] == key_id), None)


def verify(token: str | None) -> dict | None:
    """The key record for a valid, not revoked token, else None."""
    if not token:
        return None
    h = _hash(token)
    with _io:
        for k in _load():
            if secrets.compare_digest(k["hash"], h) and not k["revoked"]:
                return public(k)
    return None


def token_from_header(value: str | None) -> str | None:
    if value and value[:7].lower() == "bearer ":
        return value[7:].strip()
    return None


def record_call(key: dict, tool: str, args: dict, ok: bool, seconds: float, summary: str):
    entry = {"time": _now(), "key_id": key["id"], "key": key["name"], "tool": tool,
             "args": {k: (v if len(str(v)) < 200 else str(v)[:200] + "...") for k, v in args.items()},
             "ok": ok, "seconds": round(seconds, 3), "summary": summary}
    with _io:
        DATA.mkdir(exist_ok=True)
        with open(CALLS, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        keys = _load()
        for k in keys:
            if k["id"] == key["id"]:
                k["calls"] = k.get("calls", 0) + 1
                k["last_used"] = entry["time"]
        _save(keys)


def recent_calls(limit: int = 50) -> list[dict]:
    if not CALLS.exists():
        return []
    lines = CALLS.read_text(encoding="utf-8").splitlines()[-limit:]
    return [json.loads(x) for x in reversed(lines) if x.strip()]
