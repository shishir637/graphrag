"""Context Layer app: storage for users, sessions, agents, audit and settings (SQLite, stdlib only),
plus password hashing and tokens. Everything lives in DATA_DIR (env CL_DATA_DIR, default ./var).
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CL_DATA_DIR", ROOT / "var"))
DB_PATH = DATA_DIR / "app.db"
SECRETS_FILE = DATA_DIR / "secrets.json"
VERSION = "0.4.0"
SESSION_DAYS = 14
ROLES = ("owner", "admin", "viewer")
CAPABILITIES = ("documents", "data", "graph", "upload")
READ_CAPABILITIES = ("documents", "data", "graph")
ACCESS_TOKEN_S = 3600
REFRESH_TOKEN_DAYS = 30

_local = threading.local()
_init_lock = threading.Lock()
_initialised = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, pw_hash TEXT NOT NULL,
  role TEXT NOT NULL, created TEXT NOT NULL, last_login TEXT, disabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), created TEXT NOT NULL,
  expires TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', groups TEXT NOT NULL,
  capabilities TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL, prefix TEXT NOT NULL, created TEXT NOT NULL,
  created_by TEXT NOT NULL, last_used TEXT, calls INTEGER NOT NULL DEFAULT 0, revoked TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, time TEXT NOT NULL, actor_type TEXT NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}',
  ok INTEGER NOT NULL DEFAULT 1, seconds REAL);
CREATE INDEX IF NOT EXISTS events_time ON events(time);
CREATE INDEX IF NOT EXISTS events_actor ON events(actor_type, actor);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_clients (
  client_id TEXT PRIMARY KEY, secret_hash TEXT, name TEXT NOT NULL, redirect_uris TEXT NOT NULL,
  kind TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_codes (
  code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id),
  redirect_uri TEXT NOT NULL, challenge TEXT NOT NULL, scope TEXT NOT NULL, resource TEXT, expires TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_tokens (
  token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, family TEXT NOT NULL, client_id TEXT NOT NULL,
  client_name TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id), scope TEXT NOT NULL, resource TEXT,
  created TEXT NOT NULL, expires TEXT NOT NULL, revoked TEXT, last_used TEXT);
CREATE INDEX IF NOT EXISTS oauth_tokens_family ON oauth_tokens(family);
CREATE TABLE IF NOT EXISTS upload_links (
  token_hash TEXT PRIMARY KEY, principal TEXT NOT NULL, groups TEXT NOT NULL, created TEXT NOT NULL,
  expires TEXT NOT NULL, files INTEGER NOT NULL DEFAULT 0);
"""

MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN groups TEXT NOT NULL DEFAULT '[\"public\"]'",
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    """One connection per thread; schema created on first use."""
    global _initialised
    c = getattr(_local, "conn", None)
    if c is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    if not _initialised:
        with _init_lock:
            c.executescript(SCHEMA)
            for m in MIGRATIONS:
                try:
                    c.execute(m)
                except sqlite3.OperationalError:          # already applied
                    pass
            _initialised = True
    return c


def reset_for_tests(path: Path):
    """Point the app at a fresh data folder (tests only)."""
    global DATA_DIR, DB_PATH, SECRETS_FILE, _initialised
    DATA_DIR, DB_PATH, SECRETS_FILE = path, path / "app.db", path / "secrets.json"
    _local.conn = None
    _initialised = False


@contextmanager
def tx():
    c = db()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


# ------------------------------------------------------------------ hashing and tokens

def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${h.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, h = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got.hex(), h)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(28)


# ------------------------------------------------------------------ users and sessions

def user_count() -> int:
    return db().execute("SELECT count(*) FROM users").fetchone()[0]


def public_user(row) -> dict:
    return {k: row[k] for k in ("id", "email", "name", "role", "created", "last_login")} | \
           {"disabled": bool(row["disabled"]), "groups": json.loads(row["groups"] or '["public"]')}


def validate_password(pw: str):
    if len(pw) < 10:
        raise ValueError("Use at least 10 characters for the password.")


def create_user(email: str, name: str, password: str, role: str) -> dict:
    email = email.strip().lower()
    if "@" not in email or len(email) > 200:
        raise ValueError("Enter a valid email address.")
    if role not in ROLES:
        raise ValueError(f"Role must be one of {', '.join(ROLES)}.")
    validate_password(password)
    with tx() as c:
        if c.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise ValueError("A user with this email already exists.")
        cur = c.execute("INSERT INTO users (email, name, pw_hash, role, created) VALUES (?, ?, ?, ?, ?)",
                        (email, name.strip()[:80] or email, hash_password(password), role, now()))
        return public_user(c.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone())


def login(email: str, password: str) -> tuple[dict, str] | None:
    row = db().execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
    if row is None:
        check_password(password, hash_password("timing-equaliser"))     # same cost whether or not the user exists
        return None
    if row["disabled"] or not check_password(password, row["pw_hash"]):
        return None
    token = new_token("cls_")
    exp = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
    with tx() as c:
        c.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (token_hash(token), row["id"], now(), exp))
        c.execute("UPDATE users SET last_login = ? WHERE id = ?", (now(), row["id"]))
        c.execute("DELETE FROM sessions WHERE expires < ?", (now(),))
    return public_user(row), token


def session_user(token: str | None) -> dict | None:
    if not token:
        return None
    row = db().execute("SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
                       "WHERE s.token_hash = ? AND s.expires > ? AND u.disabled = 0",
                       (token_hash(token), now())).fetchone()
    return public_user(row) if row else None


def logout(token: str | None):
    if token:
        db().execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))


def list_users() -> list[dict]:
    return [public_user(r) for r in db().execute("SELECT * FROM users ORDER BY created")]


def update_user(user_id: int, role: str | None = None, disabled: bool | None = None,
                groups: list[str] | None = None) -> dict:
    with tx() as c:
        row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise KeyError(user_id)
        new_role = role or row["role"]
        new_disabled = row["disabled"] if disabled is None else int(disabled)
        if new_role not in ROLES:
            raise ValueError("Unknown role.")
        if row["role"] == "owner" and (new_role != "owner" or new_disabled):
            owners = c.execute("SELECT count(*) FROM users WHERE role = 'owner' AND disabled = 0").fetchone()[0]
            if owners <= 1:
                raise ValueError("There must always be at least one active owner.")
        g = json.loads(row["groups"]) if groups is None else sorted({x.strip() for x in groups if x.strip()})
        if not g:
            raise ValueError("A user needs at least one access group.")
        c.execute("UPDATE users SET role = ?, disabled = ?, groups = ? WHERE id = ?",
                  (new_role, new_disabled, json.dumps(g), user_id))
        if new_disabled:
            c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            c.execute("UPDATE oauth_tokens SET revoked = ? WHERE user_id = ? AND revoked IS NULL", (now(), user_id))
        return public_user(c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def change_password(user_id: int, old: str, new: str):
    row = db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or not check_password(old, row["pw_hash"]):
        raise ValueError("The current password is not correct.")
    validate_password(new)
    with tx() as c:
        c.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (hash_password(new), user_id))


# ------------------------------------------------------------------ agents (MCP keys)

def public_agent(row) -> dict:
    return {"id": row["id"], "name": row["name"], "description": row["description"],
            "groups": json.loads(row["groups"]), "capabilities": json.loads(row["capabilities"]),
            "prefix": row["prefix"], "created": row["created"], "created_by": row["created_by"],
            "last_used": row["last_used"], "calls": row["calls"], "revoked": row["revoked"]}


def create_agent(name: str, description: str, groups: list[str], capabilities: list[str], by: str) -> tuple[dict, str]:
    name = " ".join(name.split())[:60]
    if not name:
        raise ValueError("Give the agent a name, for example the app or team that will use it.")
    groups = sorted({g.strip() for g in groups if g.strip()})
    caps = sorted({c for c in capabilities if c in CAPABILITIES})
    if not caps:
        raise ValueError("Pick at least one thing the agent may use: documents, data or the knowledge graph.")
    if ("documents" in caps or "graph" in caps) and not groups:
        raise ValueError("Pick at least one access group for documents.")
    token = new_token("clk_")
    aid = secrets.token_hex(5)
    with tx() as c:
        c.execute("INSERT INTO agents (id, name, description, groups, capabilities, token_hash, prefix, created, "
                  "created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (aid, name, description.strip()[:300], json.dumps(groups), json.dumps(caps), token_hash(token),
                   token[:8], now(), by))
        return public_agent(c.execute("SELECT * FROM agents WHERE id = ?", (aid,)).fetchone()), token


def list_agents() -> list[dict]:
    return [public_agent(r) for r in db().execute("SELECT * FROM agents ORDER BY created DESC")]


def get_agent(aid: str) -> dict | None:
    r = db().execute("SELECT * FROM agents WHERE id = ?", (aid,)).fetchone()
    return public_agent(r) if r else None


def update_agent(aid: str, groups: list[str] | None, capabilities: list[str] | None) -> dict:
    with tx() as c:
        r = c.execute("SELECT * FROM agents WHERE id = ?", (aid,)).fetchone()
        if r is None:
            raise KeyError(aid)
        if r["revoked"]:
            raise ValueError("This agent's key is revoked.")
        g = sorted({x.strip() for x in groups if x.strip()}) if groups is not None else json.loads(r["groups"])
        caps = sorted({x for x in capabilities if x in CAPABILITIES}) if capabilities is not None else json.loads(r["capabilities"])
        if not caps:
            raise ValueError("Pick at least one capability.")
        c.execute("UPDATE agents SET groups = ?, capabilities = ? WHERE id = ?", (json.dumps(g), json.dumps(caps), aid))
        return public_agent(c.execute("SELECT * FROM agents WHERE id = ?", (aid,)).fetchone())


def revoke_agent(aid: str) -> dict:
    with tx() as c:
        if c.execute("UPDATE agents SET revoked = ? WHERE id = ? AND revoked IS NULL", (now(), aid)).rowcount == 0:
            if not c.execute("SELECT 1 FROM agents WHERE id = ?", (aid,)).fetchone():
                raise KeyError(aid)
        return public_agent(c.execute("SELECT * FROM agents WHERE id = ?", (aid,)).fetchone())


def agent_for_token(token: str | None) -> dict | None:
    if not token:
        return None
    r = db().execute("SELECT * FROM agents WHERE token_hash = ? AND revoked IS NULL", (token_hash(token),)).fetchone()
    return public_agent(r) if r else None


def touch_agent(aid: str):
    if aid.startswith("user:"):
        return
    db().execute("UPDATE agents SET calls = calls + 1, last_used = ? WHERE id = ?", (now(), aid))


# ------------------------------------------------------------------ who is calling /mcp

def principal_for_token(token: str | None, resource: str | None = None) -> dict | None:
    """An agent key (clk_...) or a token from "sign in to Context Layer" in Claude / ChatGPT (cla_...).

    Signed-in users act as themselves: their access groups (owners and admins: every group), all read
    capabilities, and upload if they are an owner or admin. A token only works for the address it was issued for."""
    if not token:
        return None
    if not token.startswith("cla_"):
        a = agent_for_token(token)
        return a | {"kind": "key"} if a else None
    r = db().execute("SELECT t.client_name, t.client_id, t.resource, u.* FROM oauth_tokens t JOIN users u "
                     "ON u.id = t.user_id WHERE t.token_hash = ? AND t.kind = 'access' AND t.revoked IS NULL "
                     "AND t.expires > ? AND u.disabled = 0", (token_hash(token), now())).fetchone()
    if r is None:
        return None
    if resource and r["resource"] and r["resource"].rstrip("/").lower() != resource.rstrip("/").lower():
        return None
    admin = r["role"] in ("owner", "admin")
    return {"id": f"user:{r['id']}", "kind": "user", "user_id": r["id"], "email": r["email"], "role": r["role"],
            "name": f"{r['name']} via {r['client_name']}", "client": r["client_name"],
            "groups": json.loads(r["groups"] or '["public"]'), "all_groups": admin,
            "capabilities": list(READ_CAPABILITIES) + (["upload"] if admin else [])}


def touch_token(token: str):
    db().execute("UPDATE oauth_tokens SET last_used = ? WHERE token_hash = ?", (now(), token_hash(token)))


# ------------------------------------------------------------------ events (activity + audit)

def log_event(actor_type: str, actor: str, action: str, target: str = "", detail: dict | None = None,
              ok: bool = True, seconds: float | None = None):
    db().execute("INSERT INTO events (time, actor_type, actor, action, target, detail, ok, seconds) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (now(), actor_type, actor, action, target[:300], json.dumps(detail or {}, ensure_ascii=False)[:4000],
                  int(ok), None if seconds is None else round(seconds, 3)))


def list_events(actor_type: str | None = None, actor: str | None = None, q: str | None = None,
                since: str | None = None, limit: int = 200) -> list[dict]:
    sql, args = "SELECT * FROM events WHERE 1=1", []
    if actor_type:
        sql += " AND actor_type = ?"; args.append(actor_type)
    if actor:
        sql += " AND actor = ?"; args.append(actor)
    if since:
        sql += " AND time >= ?"; args.append(since)
    if q:
        sql += " AND (action LIKE ? OR target LIKE ? OR detail LIKE ?)"; args += [f"%{q}%"] * 3
    sql += " ORDER BY id DESC LIMIT ?"; args.append(max(1, min(limit, 5000)))
    return [dict(r) | {"detail": json.loads(r["detail"]), "ok": bool(r["ok"])} for r in db().execute(sql, args)]


def usage_by_day(days: int = 14) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days - 1)).date().isoformat()
    rows = db().execute("SELECT substr(time, 1, 10) AS day, count(*) AS calls, sum(1 - ok) AS errors FROM events "
                        "WHERE actor_type = 'agent' AND time >= ? GROUP BY day ORDER BY day", (since,)).fetchall()
    by = {r["day"]: (r["calls"], r["errors"]) for r in rows}
    start = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    return [{"day": (start + timedelta(days=i)).isoformat(),
             "calls": by.get((start + timedelta(days=i)).isoformat(), (0, 0))[0],
             "errors": by.get((start + timedelta(days=i)).isoformat(), (0, 0))[1]} for i in range(days)]


# ------------------------------------------------------------------ settings and secrets

def get_setting(key: str, default=None):
    r = db().execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def set_setting(key: str, value):
    db().execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, json.dumps(value)))


def load_secrets():
    """API keys saved from Settings go to secrets.json (owner-only permissions) and into the environment.
    A value already set in the environment (e.g. by Docker) wins."""
    if SECRETS_FILE.exists():
        for k, v in json.loads(SECRETS_FILE.read_text(encoding="utf-8")).items():
            os.environ.setdefault(k, v)


def save_secret(name: str, value: str | None):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = json.loads(SECRETS_FILE.read_text(encoding="utf-8")) if SECRETS_FILE.exists() else {}
    if value:
        data[name] = value
        os.environ[name] = value
    else:
        data.pop(name, None)
        os.environ.pop(name, None)
    tmp = SECRETS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(SECRETS_FILE)


def mask(value: str | None) -> str | None:
    return None if not value else value[:7] + "…" + value[-4:]


def timer():
    t0 = time.perf_counter()
    return lambda: time.perf_counter() - t0
