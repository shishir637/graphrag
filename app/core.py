"""Context Layer app: workspaces (tenants), people, sessions, agent keys, audit and settings in Postgres
(see app/db.py and migrations/), plus password hashing and tokens.

A person has one account (email + password) and a role and access groups in each workspace they belong to.
Agent keys, events, settings and upload links belong to one workspace. Files for secrets still live in DATA_DIR
(env CL_DATA_DIR, default ./var) until the encrypted secrets store replaces them.
"""
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import current_tenant, db, system, tenant_scope, tx   # noqa: F401  (re-exported)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CL_DATA_DIR", ROOT / "var"))
SECRETS_FILE = DATA_DIR / "secrets.json"
VERSION = "0.5.0"
SESSION_DAYS = 14
ROLES = ("owner", "admin", "viewer")
CAPABILITIES = ("documents", "data", "graph", "upload")
READ_CAPABILITIES = ("documents", "data", "graph")
SCOPES = ("agent", "admin", "delegate")
ACCESS_TOKEN_S = 3600
REFRESH_TOKEN_DAYS = 30


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def reset_for_tests(path: Path):
    """Empty every table and point secrets at a fresh folder (tests only)."""
    global DATA_DIR, SECRETS_FILE
    import psycopg
    from . import db as D
    DATA_DIR, SECRETS_FILE = path, path / "secrets.json"
    D.migrate()
    with psycopg.connect(D.url(), autocommit=True) as c:
        tables = [r[0] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                                          "AND tablename <> 'schema_migrations'")]
        if tables:
            c.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " RESTART IDENTITY CASCADE")


def _need_tenant() -> str:
    t = current_tenant()
    if not t:
        raise RuntimeError("No workspace in scope.")
    return t


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


# ------------------------------------------------------------------ workspaces

def create_tenant(name: str) -> str:
    tid = "t_" + secrets.token_hex(6)
    with system():
        db().execute("INSERT INTO tenants (id, name, created) VALUES (?, ?, ?)", (tid, name.strip()[:80] or "Workspace", now()))
    return tid


def tenant_count() -> int:
    with system():
        return db().execute("SELECT count(*) FROM tenants").fetchone()[0]


def get_tenant(tid: str) -> dict | None:
    r = db().execute("SELECT * FROM tenants WHERE id = ?", (tid,)).fetchone()
    return {"id": r["id"], "name": r["name"], "created": r["created"], "disabled": r["disabled"]} if r else None


def rename_tenant(tid: str, name: str):
    db().execute("UPDATE tenants SET name = ? WHERE id = ?", (name.strip()[:80] or "Workspace", tid))


def user_tenants(user_id: int) -> list[dict]:
    rows = db().execute("SELECT t.id, t.name, m.role FROM memberships m JOIN tenants t ON t.id = m.tenant_id "
                        "WHERE m.user_id = ? AND m.disabled = 0 AND NOT t.disabled ORDER BY m.seq", (user_id,))
    return [{"id": r["id"], "name": r["name"], "role": r["role"]} for r in rows]


# ------------------------------------------------------------------ users and sessions

def user_count() -> int:
    return db().execute("SELECT count(*) FROM users").fetchone()[0]


_USER = ("SELECT u.id, u.email, u.name, u.pw_hash, u.created, u.last_login, "
         "(u.disabled + m.disabled) AS disabled, m.role, m.groups, m.tenant_id FROM users u "
         "JOIN memberships m ON m.user_id = u.id ")


def public_user(row) -> dict:
    return {k: row[k] for k in ("id", "email", "name", "role", "created", "last_login", "tenant_id")} | \
           {"disabled": bool(row["disabled"]), "groups": json.loads(row["groups"] or '["public"]')}


def validate_password(pw: str):
    if len(pw) < 10:
        raise ValueError("Use at least 10 characters for the password.")


def _clean_email(email: str) -> str:
    email = email.strip().lower()
    if "@" not in email or len(email) > 200:
        raise ValueError("Enter a valid email address.")
    return email


def create_user(email: str, name: str, password: str, role: str, tenant_id: str | None = None) -> dict:
    """Add a person to the workspace. Someone who already has an account (in another workspace) is added with
    their existing password."""
    email = _clean_email(email)
    tid = tenant_id or _need_tenant()
    if role not in ROLES:
        raise ValueError(f"Role must be one of {', '.join(ROLES)}.")
    with tx() as c:
        u = c.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if u is None:
            validate_password(password)
            uid = c.execute("INSERT INTO users (email, name, pw_hash, created) VALUES (?, ?, ?, ?) RETURNING id",
                            (email, name.strip()[:80] or email, hash_password(password), now())).fetchone()[0]
        else:
            uid = u["id"]
            if c.execute("SELECT 1 FROM memberships WHERE user_id = ? AND tenant_id = ?", (uid, tid)).fetchone():
                raise ValueError("A user with this email already exists.")
        c.execute("INSERT INTO memberships (user_id, tenant_id, role, created) VALUES (?, ?, ?, ?)", (uid, tid, role, now()))
        return public_user(c.execute(_USER + "WHERE u.id = ? AND m.tenant_id = ?", (uid, tid)).fetchone())


def signup(email: str, name: str, password: str, organisation: str) -> dict:
    """A new workspace with this person as its owner."""
    email = _clean_email(email)
    validate_password(password)
    if db().execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        raise ValueError("An account with this email already exists. Sign in instead.")
    tid = create_tenant(organisation or name or email)
    with tenant_scope(tid):
        set_setting("organisation", organisation.strip()[:80])
        return create_user(email, name, password, "owner", tid)


def login(email: str, password: str, tenant_id: str | None = None) -> tuple[dict, str] | None:
    email = email.strip().lower()
    row = db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if row is None:
        check_password(password, hash_password("timing-equaliser"))     # same cost whether or not the user exists
        return None
    if row["disabled"] or not check_password(password, row["pw_hash"]):
        return None
    tenants = user_tenants(row["id"])
    pick = next((t for t in tenants if t["id"] == tenant_id), None) if tenant_id else (tenants[0] if tenants else None)
    if pick is None:
        return None
    token = new_token("cls_")
    exp = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
    with tx() as c:
        c.execute("INSERT INTO sessions (token_hash, user_id, tenant_id, created, expires) VALUES (?, ?, ?, ?, ?)",
                  (token_hash(token), row["id"], pick["id"], now(), exp))
        c.execute("UPDATE users SET last_login = ? WHERE id = ?", (now(), row["id"]))
        c.execute("DELETE FROM sessions WHERE expires < ?", (now(),))
        u = c.execute(_USER + "WHERE u.id = ? AND m.tenant_id = ?", (row["id"], pick["id"])).fetchone()
    return public_user(u), token


def session_user(token: str | None) -> dict | None:
    if not token:
        return None
    row = db().execute(_USER.replace("FROM users u", "FROM sessions s JOIN users u ON u.id = s.user_id") +
                       "AND m.tenant_id = s.tenant_id JOIN tenants t ON t.id = s.tenant_id "
                       "WHERE s.token_hash = ? AND s.expires > ? AND u.disabled = 0 AND m.disabled = 0 AND NOT t.disabled",
                       (token_hash(token), now())).fetchone()
    return public_user(row) if row else None


def switch_tenant(token: str, tenant_id: str) -> dict:
    u = session_user(token)
    if u is None or tenant_id not in {t["id"] for t in user_tenants(u["id"])}:
        raise PermissionError("You are not a member of that workspace.")
    db().execute("UPDATE sessions SET tenant_id = ? WHERE token_hash = ?", (tenant_id, token_hash(token)))
    return session_user(token)


def logout(token: str | None):
    if token:
        db().execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))


def list_users() -> list[dict]:
    return [public_user(r) for r in db().execute(_USER + "WHERE m.tenant_id = ? ORDER BY m.seq", (_need_tenant(),))]


def update_user(user_id: int, role: str | None = None, disabled: bool | None = None,
                groups: list[str] | None = None) -> dict:
    tid = _need_tenant()
    with tx() as c:
        row = c.execute("SELECT * FROM memberships WHERE user_id = ? AND tenant_id = ?", (user_id, tid)).fetchone()
        if row is None:
            raise KeyError(user_id)
        new_role = role or row["role"]
        new_disabled = row["disabled"] if disabled is None else int(disabled)
        if new_role not in ROLES:
            raise ValueError("Unknown role.")
        if row["role"] == "owner" and (new_role != "owner" or new_disabled):
            owners = c.execute("SELECT count(*) FROM memberships WHERE tenant_id = ? AND role = 'owner' AND disabled = 0",
                               (tid,)).fetchone()[0]
            if owners <= 1:
                raise ValueError("There must always be at least one active owner.")
        g = json.loads(row["groups"]) if groups is None else sorted({x.strip() for x in groups if x.strip()})
        if not g:
            raise ValueError("A user needs at least one access group.")
        c.execute("UPDATE memberships SET role = ?, disabled = ?, groups = ? WHERE user_id = ? AND tenant_id = ?",
                  (new_role, new_disabled, json.dumps(g), user_id, tid))
        if new_disabled:
            c.execute("DELETE FROM sessions WHERE user_id = ? AND tenant_id = ?", (user_id, tid))
            c.execute("UPDATE oauth_tokens SET revoked = ? WHERE user_id = ? AND tenant_id = ? AND revoked IS NULL",
                      (now(), user_id, tid))
        return public_user(c.execute(_USER + "WHERE u.id = ? AND m.tenant_id = ?", (user_id, tid)).fetchone())


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
            "scope": row["scope"], "tenant_id": row["tenant_id"], "expires": row["expires"],
            "allowed_ips": json.loads(row["allowed_ips"] or "[]"),
            "prefix": row["prefix"], "created": row["created"], "created_by": row["created_by"],
            "last_used": row["last_used"], "calls": row["calls"], "revoked": row["revoked"]}


def _clean_ips(ips) -> list[str]:
    import ipaddress
    out = []
    for x in ips or []:
        try:
            out.append(str(ipaddress.ip_network(str(x).strip(), strict=False)))
        except ValueError:
            raise ValueError(f"'{x}' is not an IP address or CIDR range (e.g. 10.0.0.0/8).") from None
    return out


def _clean_expiry(expires: str | None) -> str | None:
    if not expires:
        return None
    try:
        d = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Expiry must be a date and time, e.g. 2027-01-31 or 2027-01-31T18:00:00Z.") from None
    d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def create_agent(name: str, description: str, groups: list[str], capabilities: list[str], by: str,
                 scope: str = "agent", expires: str | None = None, allowed_ips: list[str] | None = None
                 ) -> tuple[dict, str]:
    """scope agent: asks questions (and uploads if allowed). scope admin: may also set the workspace up
    (connect sources, import definitions, approve metrics) over the API or MCP. scope delegate: an app's backend
    acting for its own end users (app/delegate.py); the returned agent carries its signing secret, shown once.
    expires: the key stops working then; allowed_ips: CIDR ranges the key may be used from."""
    tid = _need_tenant()
    name = " ".join(name.split())[:60]
    if not name:
        raise ValueError("Give the agent a name, for example the app or team that will use it.")
    if scope not in SCOPES:
        raise ValueError("Scope must be agent or admin.")
    groups = sorted({g.strip() for g in groups if g.strip()})
    caps = sorted({c for c in capabilities if c in CAPABILITIES})
    if scope == "admin":
        caps = sorted(set(caps) | {"upload"})
    if not caps:
        raise ValueError("Pick at least one thing the agent may use: documents, data or the knowledge graph.")
    if ("documents" in caps or "graph" in caps) and not groups and scope != "delegate":
        raise ValueError("Pick at least one access group for documents.")
    exp, ips = _clean_expiry(expires), _clean_ips(allowed_ips)
    token = new_token("clk_")
    aid = secrets.token_hex(5)
    with tx() as c:
        c.execute("INSERT INTO agents (id, tenant_id, name, description, groups, capabilities, scope, token_hash, prefix, "
                  "created, created_by, expires, allowed_ips) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (aid, tid, name, description.strip()[:300], json.dumps(groups), json.dumps(caps), scope,
                   token_hash(token), token[:8], now(), by, exp, json.dumps(ips)))
        a = public_agent(c.execute("SELECT * FROM agents WHERE id = ?", (aid,)).fetchone())
    if scope == "delegate":
        from .delegate import new_signing_secret
        a["signing_secret"] = new_signing_secret(aid)
    return a, token


def list_agents() -> list[dict]:
    return [public_agent(r) for r in db().execute("SELECT * FROM agents ORDER BY created DESC")]


def get_agent(aid: str) -> dict | None:
    r = db().execute("SELECT * FROM agents WHERE id = ?", (aid,)).fetchone()
    return public_agent(r) if r else None


def update_agent(aid: str, groups: list[str] | None, capabilities: list[str] | None, expires: str | None = None,
                 allowed_ips: list[str] | None = None) -> dict:
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
        if expires is not None:
            c.execute("UPDATE agents SET expires = ? WHERE id = ?", (_clean_expiry(expires) if expires else None, aid))
        if allowed_ips is not None:
            c.execute("UPDATE agents SET allowed_ips = ? WHERE id = ?", (json.dumps(_clean_ips(allowed_ips)), aid))
        return public_agent(c.execute("SELECT * FROM agents WHERE id = ?", (aid,)).fetchone())


def revoke_agent(aid: str) -> dict:
    with tx() as c:
        if c.execute("UPDATE agents SET revoked = ? WHERE id = ? AND revoked IS NULL", (now(), aid)).rowcount == 0:
            if not c.execute("SELECT 1 FROM agents WHERE id = ?", (aid,)).fetchone():
                raise KeyError(aid)
        return public_agent(c.execute("SELECT * FROM agents WHERE id = ?", (aid,)).fetchone())


def agent_for_token(token: str | None, ip: str | None = None) -> dict | None:
    """Which key is this (in any workspace)? A key of a disabled workspace, an expired key, or a key used from an
    address outside its allow-list does not work."""
    if not token:
        return None
    with system():
        r = db().execute("SELECT a.* FROM agents a JOIN tenants t ON t.id = a.tenant_id "
                         "WHERE a.token_hash = ? AND a.revoked IS NULL AND NOT t.disabled", (token_hash(token),)).fetchone()
    if r is None:
        return None
    a = public_agent(r)
    if a["expires"] and a["expires"] <= now():
        return None
    if a["allowed_ips"]:
        import ipaddress
        try:
            addr = ipaddress.ip_address(ip or "")
        except ValueError:
            return None
        if not any(addr in ipaddress.ip_network(n) for n in a["allowed_ips"]):
            return None
    return a


def touch_agent(aid: str):
    if aid.startswith("user:"):
        return
    with system():
        db().execute("UPDATE agents SET calls = calls + 1, last_used = ? WHERE id = ?", (now(), aid))


# ------------------------------------------------------------------ who is calling /mcp

def principal_for_token(token: str | None, resource: str | None = None, ip: str | None = None,
                        headers: dict | None = None) -> dict | None:
    """An agent key (clk_...) or a token from "sign in to Context Layer" in Claude / ChatGPT (cla_...).

    Signed-in users act as themselves in the workspace they signed in to: their access groups (owners and admins:
    every group), all read capabilities, and upload if they are an owner or admin. A token only works for the
    address it was issued for. Every principal carries its workspace (tenant_id)."""
    if not token:
        return None
    if not token.startswith("cla_"):
        a = agent_for_token(token, ip)
        if a is None:
            return None
        if a["scope"] == "delegate":                  # acting for one of the app's end users: their groups only
            from .delegate import get_user, identify
            with tenant_scope(a["tenant_id"]):
                uid, why = identify(a["id"], headers or {})
                if uid is None:
                    raise PermissionError(why)
                u = get_user(uid)
            if u is None or u["disabled"]:
                raise PermissionError(f"Unknown or disabled user '{uid}' for this key. Add it with "
                                      "PUT /api/v1/external-users/{id}.")
            return a | {"kind": "key", "end_user": uid, "groups": u["groups"], "key_name": a["name"],
                        "name": f"{a['name']} for {uid}", "capabilities": [c for c in a["capabilities"] if c != "upload"]}
        return a | {"kind": "key"}
    r = db().execute("SELECT t.client_name, t.client_id, t.resource, t.tenant_id, u.id, u.email, u.name, m.role, m.groups "
                     "FROM oauth_tokens t JOIN users u ON u.id = t.user_id "
                     "JOIN memberships m ON m.user_id = u.id AND m.tenant_id = t.tenant_id "
                     "JOIN tenants w ON w.id = t.tenant_id "
                     "WHERE t.token_hash = ? AND t.kind = 'access' AND t.revoked IS NULL AND t.expires > ? "
                     "AND u.disabled = 0 AND m.disabled = 0 AND NOT w.disabled", (token_hash(token), now())).fetchone()
    if r is None:
        return None
    if resource and r["resource"] and r["resource"].rstrip("/").lower() != resource.rstrip("/").lower():
        return None
    admin = r["role"] in ("owner", "admin")
    return {"id": f"user:{r['id']}", "kind": "user", "user_id": r["id"], "email": r["email"], "role": r["role"],
            "name": f"{r['name']} via {r['client_name']}", "client": r["client_name"], "tenant_id": r["tenant_id"],
            "scope": "admin" if admin else "agent",
            "groups": json.loads(r["groups"] or '["public"]'), "all_groups": admin,
            "capabilities": list(READ_CAPABILITIES) + (["upload"] if admin else [])}


def touch_token(token: str):
    db().execute("UPDATE oauth_tokens SET last_used = ? WHERE token_hash = ?", (now(), token_hash(token)))


# ------------------------------------------------------------------ events (activity + audit)

def log_event(actor_type: str, actor: str, action: str, target: str = "", detail: dict | None = None,
              ok: bool = True, seconds: float | None = None):
    """Record in the current workspace's log; events outside any workspace (e.g. a failed sign-in) go to the
    server log (tenant '')."""
    tid = current_tenant() or ""
    if tid and actor_type == "agent" and action.startswith("tool.") and get_setting("redact_questions", False):
        target, detail = redact(target), _redact_detail(detail or {})
    args = (tid, now(), actor_type, actor, action, target[:300], json.dumps(detail or {}, ensure_ascii=False)[:4000],
            int(ok), None if seconds is None else round(seconds, 3))
    sql = ("INSERT INTO events (tenant_id, time, actor_type, actor, action, target, detail, ok, seconds) "
           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")
    if tid:
        db().execute(sql, args)
    else:
        with system():
            db().execute(sql, args)


def redact(text: str) -> str:
    """What is stored instead of a question when the workspace keeps question text out of the log."""
    if not text:
        return text
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()[:16]} ({len(text)} chars)"


def _redact_detail(d: dict) -> dict:
    d = dict(d)
    if isinstance(d.get("args"), dict):
        d["args"] = {k: (redact(v) if k in ("question", "query", "text") and isinstance(v, str) else v)
                     for k, v in d["args"].items()}
    return d


def purge_events(days: int) -> int:
    """Delete this workspace's events older than `days` (audit retention)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat(timespec="seconds")
    return db().execute("DELETE FROM events WHERE tenant_id = ? AND time < ?", (_need_tenant(), cutoff)).rowcount


def list_events(actor_type: str | None = None, actor: str | None = None, q: str | None = None,
                since: str | None = None, limit: int = 200) -> list[dict]:
    sql, args = "SELECT * FROM events WHERE tenant_id = ?", [_need_tenant()]
    if actor_type:
        sql += " AND actor_type = ?"; args.append(actor_type)
    if actor:
        sql += " AND actor = ?"; args.append(actor)
    if since:
        sql += " AND time >= ?"; args.append(since)
    if q:
        sql += " AND (action ILIKE ? OR target ILIKE ? OR detail ILIKE ?)"; args += [f"%{q}%"] * 3
    sql += " ORDER BY id DESC LIMIT ?"; args.append(max(1, min(limit, 5000)))
    return [{k: r[k] for k in r.keys() if k != "tenant_id"} | {"detail": json.loads(r["detail"]), "ok": bool(r["ok"])}
            for r in db().execute(sql, args)]


def usage_by_day(days: int = 14) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days - 1)).date().isoformat()
    rows = db().execute("SELECT substr(time, 1, 10) AS day, count(*) AS calls, sum(1 - ok) AS errors FROM events "
                        "WHERE tenant_id = ? AND actor_type = 'agent' AND time >= ? GROUP BY day ORDER BY day",
                        (_need_tenant(), since)).fetchall()
    by = {r["day"]: (r["calls"], r["errors"]) for r in rows}
    start = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    return [{"day": (start + timedelta(days=i)).isoformat(),
             "calls": by.get((start + timedelta(days=i)).isoformat(), (0, 0))[0],
             "errors": int(by.get((start + timedelta(days=i)).isoformat(), (0, 0))[1] or 0)} for i in range(days)]


# ------------------------------------------------------------------ settings and secrets

def get_setting(key: str, default=None):
    """A setting of the current workspace (default when no workspace is in scope)."""
    tid = current_tenant()
    if not tid:
        return default
    r = db().execute("SELECT value FROM settings WHERE tenant_id = ? AND key = ?", (tid, key)).fetchone()
    return json.loads(r["value"]) if r else default


def set_setting(key: str, value):
    db().execute("INSERT INTO settings (tenant_id, key, value) VALUES (?, ?, ?) "
                 "ON CONFLICT (tenant_id, key) DO UPDATE SET value = excluded.value",
                 (_need_tenant(), key, json.dumps(value)))


def get_system_setting(key: str, default=None):
    r = db().execute("SELECT value FROM system_settings WHERE key = ?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def set_system_setting(key: str, value):
    db().execute("INSERT INTO system_settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))


def load_secrets():
    """API keys saved from Settings go to secrets.json (owner-only permissions) and into the environment.
    A value already set in the environment (e.g. by Docker) wins."""
    if SECRETS_FILE.exists():
        for k, v in json.loads(SECRETS_FILE.read_text(encoding="utf-8")).items():
            os.environ.setdefault(k, v)


def save_secret(name: str, value: str | None):
    """Inside a workspace: encrypted in Postgres for that workspace only (app/vault.py)."""
    if current_tenant():
        from . import vault
        vault.put(name, value)
        return
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
