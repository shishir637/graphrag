"""Keys for apps (0.9): a key with scope "delegate" lets an app's backend (e.g. CodeVerse) call Context Layer for
each of its own end users. Every request says WHO the end user is, proven with the key's signing secret (shown once
when the key is made). The call then gets that user's access only: the data sets and roles given to the app user
here (Settings -> Keys for apps), never more than the key itself has. Without a valid identity the call is refused
with the reason. Ported from dev app/delegate.py.

Two ways to say who the user is:
  1. Signed headers, one-time:  X-CL-User: <user id>   X-CL-User-Ts: <unix seconds>   X-CL-User-Nonce: <random>
                                X-CL-User-Sig: hex(HMAC-SHA256(secret, "<user id>.<ts>.<nonce>"))
     valid for 5 minutes and only once (replays are refused).
  2. A short-lived token:       X-CL-User-Token: <JWT, HS256, signed with the same secret>
     claims: sub (user id), iat, exp (at most 15 minutes after iat). Reusable until it expires.

Keys can also expire and be limited to IP ranges (app/core.py: expires, allowed_ips; the client address, or the
first X-Forwarded-For address with CL_TRUST_PROXY=1).
"""
import base64
import hashlib
import hmac
import json
import time

from . import core, vault
from . import db as D

WINDOW_S = 300
MAX_TOKEN_S = 900
SCHEMA = """
CREATE TABLE IF NOT EXISTS external_users (
  user_id TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', datasets TEXT NOT NULL DEFAULT '[]',
  roles TEXT NOT NULL DEFAULT '[]', disabled INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL, updated TEXT NOT NULL,
  key_id TEXT{tenant}, PRIMARY KEY ({tk}user_id));
CREATE TABLE IF NOT EXISTS used_signatures (sig TEXT PRIMARY KEY, expires TEXT NOT NULL);
"""


def _db():
    return core.db_with(SCHEMA)


def secret_name(aid: str) -> str:
    return f"key:{aid}:signing"


def new_signing_secret(aid: str) -> str:
    import secrets
    s = "cls_sign_" + secrets.token_urlsafe(32)
    vault.put(secret_name(aid), s)
    return s


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_headers(user_id: str, secret: str, ts: int | None = None) -> dict:
    """What the app's backend sends (also used in tests and the docs)."""
    import secrets
    ts = int(ts if ts is not None else time.time())
    nonce = secrets.token_hex(8)
    sig = hmac.new(secret.encode(), f"{user_id}.{ts}.{nonce}".encode(), hashlib.sha256).hexdigest()
    return {"x-cl-user": user_id, "x-cl-user-ts": str(ts), "x-cl-user-nonce": nonce, "x-cl-user-sig": sig}


def make_token(user_id: str, secret: str, ttl: int = 600, now: int | None = None) -> str:
    now = int(now if now is not None else time.time())
    head = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64url(json.dumps({"sub": user_id, "iat": now, "exp": now + ttl}).encode())
    sig = _b64url(hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


def _used(sig: str) -> bool:
    """Record a one-time signature; True when it was used before (a replay)."""
    from datetime import datetime, timedelta, timezone
    exp = (datetime.now(timezone.utc) + timedelta(seconds=2 * WINDOW_S)).isoformat(timespec="seconds")
    with core.system():
        c = _db()
        c.execute("DELETE FROM used_signatures WHERE expires < ?", (core.now(),))
        r = c.execute("INSERT INTO used_signatures (sig, expires) VALUES (?, ?) ON CONFLICT (sig) DO NOTHING "
                      "RETURNING sig", (hashlib.sha256(sig.encode()).hexdigest(), exp)).fetchone()
    return r is None


def identify(aid: str, headers: dict) -> tuple[str | None, str]:
    """(end user id, "") when the request proves who the user is, else (None, reason)."""
    secret = vault.get(secret_name(aid))
    if not secret:
        return None, "This key has no signing secret."
    h = {k.lower(): v for k, v in (headers or {}).items()}
    now = int(time.time())
    tok = h.get("x-cl-user-token")
    if tok:
        try:
            head, body, sig = tok.split(".")
            if json.loads(_unb64(head)).get("alg") != "HS256":
                return None, "Only HS256 user tokens are accepted."
            good = _b64url(hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(good, sig):
                return None, "The user token's signature is not valid."
            c = json.loads(_unb64(body))
            iat, exp = int(c.get("iat", 0)), int(c.get("exp", 0))
            if exp < now or iat > now + 60 or exp - iat > MAX_TOKEN_S:
                return None, "The user token is expired or lives too long (at most 15 minutes)."
            uid = str(c.get("sub") or "")
            return (uid, "") if uid else (None, "The user token has no sub.")
        except (ValueError, TypeError, json.JSONDecodeError):
            return None, "The user token cannot be read."
    uid, ts, sig = h.get("x-cl-user"), h.get("x-cl-user-ts"), h.get("x-cl-user-sig")
    if not (uid and ts and sig):
        return None, "This key acts for end users: send X-CL-User, X-CL-User-Ts and X-CL-User-Sig (or X-CL-User-Token)."
    try:
        t = int(ts)
    except ValueError:
        return None, "X-CL-User-Ts must be unix seconds."
    if abs(now - t) > WINDOW_S:
        return None, "The signed user identity is older than 5 minutes."
    nonce = h.get("x-cl-user-nonce")
    msg = f"{uid}.{t}.{nonce}" if nonce else f"{uid}.{t}"
    good = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(good, sig.lower()):
        return None, "The user identity's signature is not valid."
    if _used(sig.lower()):
        return None, "This signed user identity was already used (replay)."
    return uid, ""


def act_for(key: dict, headers: dict) -> dict:
    """The caller behind a delegate key for this request: that end user's data sets (and their roles' data sets),
    never more than the key has, with the key's connectors. Raises PermissionError with the reason."""
    uid, why = identify(key["id"], headers)
    if uid is None:
        raise PermissionError(why)
    u = get_user(uid)
    if u is None or u["disabled"]:
        raise PermissionError(f"The app user {uid} is not known here (or disabled). Add them under Keys for apps.")
    from . import access
    ds = set(u["datasets"])
    rmap = {r["name"]: r for r in access.roles()}
    caps = set(key["capabilities"])
    for rn in u["roles"]:
        if rn in rmap:
            ds |= set(rmap[rn]["datasets"])
    if key.get("groups"):
        ds &= set(key["groups"])
    return key | {"groups": sorted(ds) or ["__none__"], "capabilities": sorted(caps - {"upload"}),
                  "external_user": uid, "name": f"{key['name']} for {u['name'] or uid}"}


# ------------------------------------------------------------------ the app's end users

def get_user(user_id: str) -> dict | None:
    r = _db().execute("SELECT * FROM external_users WHERE user_id = ?", (user_id,)).fetchone()
    return {"user_id": r["user_id"], "name": r["name"], "datasets": json.loads(r["datasets"]),
            "roles": json.loads(r["roles"]), "disabled": bool(r["disabled"]), "created": r["created"],
            "updated": r["updated"]} if r else None


def put_user(user_id: str, datasets: list[str], roles: list[str] | None = None, name: str = "",
             disabled: bool = False) -> dict:
    from . import access
    from .engine import clean_groups
    uid = str(user_id).strip()[:200]
    if not uid:
        raise ValueError("Give the user id the app uses.")
    ds = clean_groups(datasets)
    known = {r["name"] for r in access.roles()}
    bad = [r for r in roles or [] if r not in known]
    if bad:
        raise ValueError(f"Unknown role: {', '.join(bad)}")
    _db().execute(f"INSERT INTO external_users (user_id, name, datasets, roles, disabled, created, updated) "
                  f"VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT {D.ck('user_id')} DO UPDATE SET name = excluded.name, "
                  "datasets = excluded.datasets, roles = excluded.roles, disabled = excluded.disabled, "
                  "updated = excluded.updated",
                  (uid, name[:120], json.dumps(ds), json.dumps(sorted(set(roles or []))), int(disabled), core.now(),
                   core.now()))
    return get_user(uid)


def delete_user(user_id: str) -> bool:
    return _db().execute("DELETE FROM external_users WHERE user_id = ?", (user_id,)).rowcount > 0


def list_users() -> list[dict]:
    return [get_user(r["user_id"]) for r in _db().execute("SELECT user_id FROM external_users ORDER BY user_id")]
