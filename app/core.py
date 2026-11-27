"""Context Layer app: storage for users, sessions, agents, audit and settings, plus password hashing and tokens.
The tables live in Postgres when CL_DATABASE_URL is set, else in SQLite at DATA_DIR/app.db (see app/db.py). Files
(secrets.json, backups) live in DATA_DIR (env CL_DATA_DIR, default ./var).
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
VERSION = "0.8.0"
SESSION_DAYS = 1                       # absolute lifetime of a web session (cookie max-age too)
SESSION_IDLE_MIN = int(os.environ.get("CL_SESSION_IDLE_MIN", "120"))   # signed out after this long without use
SCRYPT_N = int(os.environ.get("CL_SCRYPT_N", str(2 ** 17)))           # OWASP: scrypt N=2^17, r=8, p=1
MIN_PASSWORD = int(os.environ.get("CL_MIN_PASSWORD_LENGTH", "15"))     # NIST 800-63B: 15 without a second factor
ROLES = ("owner", "admin", "publisher", "viewer")       # viewer = member
CAPABILITIES = ("documents", "data", "graph", "upload")
READ_CAPABILITIES = ("documents", "data", "graph")
ACCESS_TOKEN_S = 3600
REFRESH_TOKEN_DAYS = 30

_local = threading.local()
_init_lock = threading.Lock()
_initialised = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS users (
  id {serial}, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, pw_hash TEXT NOT NULL,
  role TEXT NOT NULL, created TEXT NOT NULL, last_login TEXT, disabled INTEGER NOT NULL DEFAULT 0,
  groups TEXT NOT NULL DEFAULT '["public"]', must_change INTEGER NOT NULL DEFAULT 0{tenant});
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, created TEXT NOT NULL,
  expires TEXT NOT NULL, last_seen TEXT);
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', groups TEXT NOT NULL,
  capabilities TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL, prefix TEXT NOT NULL, created TEXT NOT NULL,
  created_by TEXT NOT NULL, last_used TEXT, calls INTEGER NOT NULL DEFAULT 0, revoked TEXT{tenant});
CREATE TABLE IF NOT EXISTS events (
  id {serial}, time TEXT NOT NULL, actor_type TEXT NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}',
  ok INTEGER NOT NULL DEFAULT 1, seconds {real}{tenant});
CREATE INDEX IF NOT EXISTS events_time ON events(time);
CREATE INDEX IF NOT EXISTS events_actor ON events(actor_type, actor);
CREATE TABLE IF NOT EXISTS kv (key TEXT NOT NULL, value TEXT NOT NULL{tenant}, PRIMARY KEY ({tk}key));
CREATE TABLE IF NOT EXISTS oauth_clients (
  client_id TEXT PRIMARY KEY, secret_hash TEXT, name TEXT NOT NULL, redirect_uris TEXT NOT NULL,
  kind TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_codes (
  code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  redirect_uri TEXT NOT NULL, challenge TEXT NOT NULL, scope TEXT NOT NULL, resource TEXT, expires TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_tokens (
  token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, family TEXT NOT NULL, client_id TEXT NOT NULL,
  client_name TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, scope TEXT NOT NULL,
  resource TEXT, created TEXT NOT NULL, expires TEXT NOT NULL, revoked TEXT, last_used TEXT);
CREATE INDEX IF NOT EXISTS oauth_tokens_family ON oauth_tokens(family);
CREATE TABLE IF NOT EXISTS upload_links (
  token_hash TEXT PRIMARY KEY, principal TEXT NOT NULL, groups TEXT NOT NULL, created TEXT NOT NULL,
  expires TEXT NOT NULL, files INTEGER NOT NULL DEFAULT 0{tenant});
"""

MIGRATIONS = [                       # columns added after 0.5 to SQLite files made by older versions
    "ALTER TABLE users ADD COLUMN groups TEXT NOT NULL DEFAULT '[\"public\"]'",
    "ALTER TABLE users ADD COLUMN must_change INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE sessions ADD COLUMN last_seen TEXT",
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_key() -> str:
    """Which database the app is on (schemas are created once per database)."""
    from . import db as D
    return "pg:" + D.url() if D.postgres() else str(DB_PATH)


_schemas: dict[str, set] = {}


def ensure(schema: str):
    """Create a module's tables (CREATE ... IF NOT EXISTS) once per database."""
    from . import db as D
    key = db_key()
    done = _schemas.setdefault(key, set())
    if schema in done:
        return
    with _init_lock:
        if schema in done:
            return
        if D.postgres():
            with D.ddl_tx() as c:
                c.execute("SELECT pg_advisory_xact_lock(727002)")      # several processes start at once
                c.executescript(D.ddl(schema))
                c.execute(D.RLS)
                D.ensure_role(c)
        else:
            _sqlite().executescript(D.ddl(schema))
        done.add(schema)


def _sqlite() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is not None and getattr(_local, "path", None) != DB_PATH:   # the data folder moved (tests): reconnect
        c = None
    if c is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn, _local.path = c, DB_PATH
    return c


def db():
    """The database: a Postgres pool (each execute is its own transaction) or this thread's SQLite connection.
    The core tables are created on first use."""
    global _initialised
    from . import db as D
    key = db_key()
    if _initialised != key:
        with _init_lock:
            if _initialised != key:
                if D.postgres():
                    with D.ddl_tx() as c:
                        c.execute("SELECT pg_advisory_xact_lock(727002)")
                        c.executescript(D.ddl(SCHEMA))
                        c.execute(D.RLS)
                        D.ensure_role(c)
                        c.execute("INSERT INTO tenants (id, name, created) VALUES (?, ?, ?) ON CONFLICT (id) DO NOTHING",
                                  (D.DEFAULT, "First workspace", now()))
                else:
                    c = _sqlite()
                    c.executescript(D.ddl(SCHEMA))
                    c.execute("INSERT INTO tenants (id, name, created) VALUES (?, ?, ?) ON CONFLICT (id) DO NOTHING",
                              (D.DEFAULT, "First workspace", now()))
                    for m in MIGRATIONS:
                        try:
                            c.execute(m)
                        except sqlite3.OperationalError:          # already applied
                            pass
                _initialised = key
    if D.postgres():
        try:
            from .migrate_local import migrate_once
            migrate_once()
        except Exception as e:                                    # noqa: BLE001 - reported, the app still starts
            print(f"Copying var/app.db into Postgres failed: {e}", flush=True)
    return D.PgAuto() if D.postgres() else _sqlite()


_generation = 0


def db_generation() -> str:
    """Changes whenever the database is replaced or emptied (seed rows are written again)."""
    return f"{db_key()}#{_generation}"


# ------------------------------------------------------------------ workspaces

def current_tenant() -> str:
    from . import db as D
    return D.current_tenant()


def tenant_scope(tenant_id: str | None):
    from . import db as D
    return D.tenant_scope(tenant_id)


def system():
    from . import db as D
    return D.system()


def create_tenant(name: str) -> str:
    """A new workspace (a company). Returns its id. Its owner is created by the caller inside tenant_scope(id)."""
    import re as _re
    name = " ".join((name or "").split())[:80]
    if not name:
        raise ValueError("Give the organisation a name.")
    base = _re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "ws"
    tid = f"{base}-{secrets.token_hex(3)}"
    with system():
        db().execute("INSERT INTO tenants (id, name, created) VALUES (?, ?, ?)", (tid, name, now()))
    with tenant_scope(tid):
        set_setting("organisation", name)
        try:
            from rag.wspath import seed_workspace
            seed_workspace(tid)
        except ImportError:
            pass
    return tid


def tenant_of_user(user_id: int) -> str:
    with system():
        r = db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _tenant(r)


def _tenant(row) -> str:
    from . import db as D
    try:
        return row["tenant_id"] or D.DEFAULT
    except (IndexError, KeyError, TypeError):
        return D.DEFAULT


def list_tenants() -> list[dict]:
    with system():
        return [dict(r) for r in db().execute("SELECT * FROM tenants ORDER BY created")]


def db_with(schema: str):
    """db() with a module's own tables created first."""
    c = db()
    ensure(schema)
    return c


def reset_for_tests(path: Path):
    """Point the app at a fresh data folder and an empty database (tests only)."""
    global DATA_DIR, DB_PATH, SECRETS_FILE, _initialised, _generation
    DATA_DIR, DB_PATH, SECRETS_FILE = path, path / "app.db", path / "secrets.json"
    _local.conn = None
    _initialised = False
    _generation += 1
    from . import db as D
    if D.postgres():
        from . import migrate_local
        _schemas.pop(db_key(), None)
        migrate_local._done.add(db_key() + "|" + str(DATA_DIR))     # no copy before the tables are emptied
        db()
        D.truncate_all()
        with system():
            db().execute("INSERT INTO tenants (id, name, created) VALUES (?, ?, ?)", (D.DEFAULT, "First workspace", now()))
        migrate_local._done.clear()
    else:
        _schemas.clear()


@contextmanager
def tx():
    """One transaction (Postgres: a pooled connection; SQLite: BEGIN IMMEDIATE on this thread's connection)."""
    from . import db as D
    if D.postgres():
        db()
        with D.pg_tx() as c:
            yield c
        return
    c = db()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


# ------------------------------------------------------------------ hashing and tokens

def _scrypt(pw: str, salt: bytes, n: int, r: int = 8, p: int = 1) -> bytes:
    return hashlib.scrypt(pw.encode(), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=2 * 128 * r * n + 2 ** 20)


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    return f"scrypt${SCRYPT_N}$8$1${salt.hex()}${_scrypt(pw, salt, SCRYPT_N).hex()}"


_DUMMY: list[str] = []


def _dummy_hash() -> str:
    if not _DUMMY:
        _DUMMY.append(hash_password(secrets.token_hex(8)))
    return _DUMMY[0]


def needs_rehash(stored: str) -> bool:
    """Hashes made with weaker settings are upgraded the next time the person signs in."""
    try:
        return int(stored.split("$")[1]) < SCRYPT_N
    except (IndexError, ValueError):
        return True


def check_password(pw: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, h = stored.split("$")
        got = _scrypt(pw, bytes.fromhex(salt), int(n), int(r), int(p))
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
           {"disabled": bool(row["disabled"]), "groups": json.loads(row["groups"] or '["public"]'), "tenant_id": _tenant(row),
            "must_change": bool(row["must_change"]) if "must_change" in row.keys() else False}


def breached(pw: str) -> bool:
    """Is the password in a known data breach? Uses the free Have I Been Pwned range API: only the first 5
    characters of the SHA-1 hash leave this server (k-anonymity). Fails open if the service cannot be reached."""
    if os.environ.get("CL_BREACH_CHECK", "1") == "0":
        return False
    import urllib.request
    h = hashlib.sha1(pw.encode()).hexdigest().upper()
    try:
        req = urllib.request.Request(f"https://api.pwnedpasswords.com/range/{h[:5]}",
                                     headers={"Add-Padding": "true", "User-Agent": "context-layer"})
        with urllib.request.urlopen(req, timeout=3) as r:
            body = r.read().decode()
    except Exception:                                      # noqa: BLE001 - offline: do not block sign-up
        return False
    return any(line.split(":")[0] == h[5:] and line.split(":")[1].strip() != "0" for line in body.splitlines())


def validate_password(pw: str):
    if len(pw or "") < MIN_PASSWORD:
        raise ValueError(f"Use at least {MIN_PASSWORD} characters. A short sentence you will remember works well.")
    if len(pw) > 256:
        raise ValueError("Use at most 256 characters.")
    if breached(pw):
        raise ValueError("This password has appeared in a data breach elsewhere. Choose a different one.")


def create_user(email: str, name: str, password: str, role: str) -> dict:
    email = email.strip().lower()
    if "@" not in email or len(email) > 200 or email.endswith("@guest.local"):
        raise ValueError("Enter a valid email address.")
    if role not in ROLES:
        raise ValueError(f"Role must be one of {', '.join(ROLES)}.")
    validate_password(password)
    with system():
        taken = db().execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone()
    if taken:
        raise ValueError("A user with this email already exists.")
    with tx() as c:
        uid = c.execute("INSERT INTO users (email, name, pw_hash, role, created) VALUES (?, ?, ?, ?, ?) RETURNING id",
                        (email, name.strip()[:80] or email, hash_password(password), role, now())).fetchone()[0]
        return public_user(c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())


def login(email: str, password: str) -> tuple[dict, str] | None:
    with system():
        return _login(email, password)


def _login(email: str, password: str) -> tuple[dict, str] | None:
    row = db().execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
    if row is None:
        check_password(password, _dummy_hash())                       # same cost whether or not the user exists
        return None
    if row["disabled"] or row["email"].endswith("@guest.local") or not check_password(password, row["pw_hash"]):
        return None
    token = new_token("cls_")
    exp = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
    with tx() as c:
        if needs_rehash(row["pw_hash"]):
            c.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (hash_password(password), row["id"]))
        c.execute("INSERT INTO sessions (token_hash, user_id, created, expires, last_seen) VALUES (?, ?, ?, ?, ?)",
                  (token_hash(token), row["id"], now(), exp, now()))
        c.execute("UPDATE users SET last_login = ? WHERE id = ?", (now(), row["id"]))
        c.execute("DELETE FROM sessions WHERE expires < ?", (now(),))
    return public_user(row), token


def session_user(token: str | None) -> dict | None:
    if not token:
        return None
    with system():
        return _session_user(token)


def _session_user(token: str) -> dict | None:
    th = token_hash(token)
    idle = (datetime.now(timezone.utc) - timedelta(minutes=SESSION_IDLE_MIN)).isoformat(timespec="seconds")
    row = db().execute("SELECT u.*, s.last_seen AS s_seen FROM sessions s JOIN users u ON u.id = s.user_id "
                       "WHERE s.token_hash = ? AND s.expires > ? AND u.disabled = 0 "
                       "AND COALESCE(s.last_seen, s.created) > ?", (th, now(), idle)).fetchone()
    if row is None:
        return None
    t = now()
    if (row["s_seen"] or "")[:16] != t[:16]:                       # at most one write a minute
        db().execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (t, th))
    return public_user(row)


def logout_everywhere(user_id: int, apps: bool = False) -> dict:
    """End every web session of this person; with apps, also disconnect Claude/ChatGPT."""
    with tx() as c:
        n = c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,)).rowcount
        a = c.execute("UPDATE oauth_tokens SET revoked = ? WHERE user_id = ? AND revoked IS NULL",
                      (now(), user_id)).rowcount if apps else 0
    return {"sessions": n, "apps": a}


def logout(token: str | None):
    if token:
        with system():
            db().execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))


def list_users() -> list[dict]:
    """People with accounts (not the hidden guest accounts of chat-without-sign-in)."""
    return [public_user(r) for r in db().execute("SELECT * FROM users WHERE email NOT LIKE '%@guest.local' ORDER BY created")]


def update_user(user_id: int, role: str | None = None, disabled: bool | None = None,
                groups: list[str] | None = None) -> dict:
    with tx() as c:
        row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None or row["email"].endswith("@guest.local"):
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
        c.execute("UPDATE users SET pw_hash = ?, must_change = 0 WHERE id = ?", (hash_password(new), user_id))


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
    with system():
        r = db().execute("SELECT * FROM agents WHERE token_hash = ? AND revoked IS NULL", (token_hash(token),)).fetchone()
    return public_agent(r) | {"tenant_id": _tenant(r)} if r else None


def touch_agent(aid: str):
    if aid.startswith("user:"):
        return
    with system():
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
    with system():
        r = db().execute("SELECT t.client_name, t.client_id, t.resource, u.* FROM oauth_tokens t JOIN users u "
                     "ON u.id = t.user_id WHERE t.token_hash = ? AND t.kind = 'access' AND t.revoked IS NULL "
                         "AND t.expires > ? AND u.disabled = 0", (token_hash(token), now())).fetchone()
    if r is None:
        return None
    tid = _tenant(r)
    if resource and r["resource"] and r["resource"].rstrip("/").lower() != resource.rstrip("/").lower():
        return None
    admin = r["role"] in ("owner", "admin")
    groups = json.loads(r["groups"] or '["public"]')
    caps = list(READ_CAPABILITIES) + ["upload"]
    try:                                          # data sets and connectors: Everyone + teams + roles + own grants
        from .access import for_agent
        with tenant_scope(tid):
            groups, caps = for_agent(r["email"], r["role"])
    except Exception:                             # noqa: BLE001 - access tables not reachable: old behaviour
        pass
    return {"id": f"user:{r['id']}", "kind": "user", "tenant_id": tid, "user_id": r["id"], "email": r["email"], "role": r["role"],
            "name": f"{r['name']} via {r['client_name']}", "client": r["client_name"],
            "groups": groups or ["public"], "all_groups": admin, "capabilities": caps}


def set_password(user_id: int, password: str, must_change: bool = False):
    if must_change:                       # a temporary password the person replaces at first sign-in
        if len(password or "") < 10:
            raise ValueError("Use a temporary password of at least 10 characters.")
    else:
        validate_password(password)
    with tx() as c:
        c.execute("UPDATE users SET pw_hash = ?, must_change = ? WHERE id = ?", (hash_password(password), int(must_change), user_id))
        if must_change:
            c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def touch_token(token: str):
    with system():
        _touch_token(token)


def _touch_token(token: str):
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
        sql += " AND (lower(action) LIKE ? OR lower(target) LIKE ? OR lower(detail) LIKE ?)"; args += [f"%{q.lower()}%"] * 3
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
    from . import db as D
    db().execute(f"INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT {D.ck('key')} DO UPDATE SET value = excluded.value",
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
