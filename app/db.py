"""The app's database: Postgres in production, SQLite for a quick local run and the default test suite.

Postgres when CL_DATABASE_URL (or CL_DATABASE_URL_FILE) is set; a password from CL_DB_PASSWORD (or
CL_DB_PASSWORD_FILE) is put into a URL that has none. Docker Compose sets it, so accounts, sessions, agents,
activity, settings, the access tables (ac_*), teams, the directory, approvals, chats, sources and quality runs all
live in Postgres next to the definitions store (schema cl_defs). Without it, the app keeps using var/app.db.

Both backends take the same SQL: `?` placeholders, sqlite-like rows (row["name"], row[0], dict(row), row.keys()),
ON CONFLICT upserts and RETURNING. Table definitions use {serial} for an auto-numbered id column (INTEGER PRIMARY
KEY in SQLite, BIGSERIAL PRIMARY KEY in Postgres), {real} for floating point numbers, {tenant} at the end of the
column list of a workspace table and {tk} at the start of a key that includes the workspace (both empty in SQLite,
which holds one workspace).

An existing var/app.db is copied into Postgres once, idempotently (app/migrate_local.py).
"""
import os
import re
import threading
from contextlib import contextmanager


def url() -> str:
    from urllib.parse import quote, urlsplit, urlunsplit
    from .envfile import env
    u = env("CL_DATABASE_URL")
    if not u:
        return ""
    pw = env("CL_DB_PASSWORD")
    if pw:
        p = urlsplit(u)
        if p.username and p.password is None:
            host = (p.hostname or "") + (f":{p.port}" if p.port else "")
            u = urlunsplit((p.scheme, f"{p.username}:{quote(pw, safe='')}@{host}", p.path, p.query, p.fragment))
    return u


def postgres() -> bool:
    return bool(url())


def ddl(sql: str) -> str:
    """A table definition for the current backend."""
    pg = postgres()
    return (sql.replace("{serial}", "BIGSERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY")
               .replace("{real}", "DOUBLE PRECISION" if pg else "REAL")
               .replace("{tenant}", TENANT_COL if pg else "")
               .replace("{tk}", "tenant_id, " if pg else ""))


def ck(*cols: str) -> str:
    """An ON CONFLICT target for a table whose key includes the workspace on Postgres: ck("key") -> (tenant_id, key)."""
    return "(" + ", ".join((["tenant_id"] if postgres() else []) + list(cols)) + ")"


def statements(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]


# ------------------------------------------------------------------ Postgres

class Row(tuple):
    """A result row usable like sqlite3.Row: row[0], row["name"], dict(row), row.keys()."""
    __slots__ = ()
    _names: tuple = ()

    def __getitem__(self, k):
        if isinstance(k, str):
            try:
                return tuple.__getitem__(self, self._names.index(k))
            except ValueError:
                raise IndexError(k) from None
        return tuple.__getitem__(self, k)

    def keys(self):
        return list(self._names)


def _row_factory(cursor):
    names = tuple(d.name for d in cursor.description or ())
    cls = type("Row", (Row,), {"_names": names, "__slots__": ()})
    return lambda values: cls(values)


class Result:
    def __init__(self, rows: list, rowcount: int):
        self._rows, self.rowcount = rows, rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


_QMARK = re.compile(r"\?")


def _sql(q: str) -> str:
    """sqlite-style ? placeholders -> %s; a literal % must be %% for psycopg."""
    return _QMARK.sub("%s", q.replace("%", "%%"))


_pool = None
_pool_url = None
_pool_lock = threading.Lock()


def pool():
    global _pool, _pool_url
    u = url()
    if _pool is None or _pool_url != u:
        with _pool_lock:
            if _pool is None or _pool_url != u:
                from psycopg_pool import ConnectionPool
                if _pool is not None:
                    _pool.close()
                _pool = ConnectionPool(u, min_size=1, max_size=int(os.environ.get("CL_DB_POOL", "10")),
                                       configure=_configure, open=True, timeout=30)
                _pool_url = u
    return _pool


def _configure(conn):
    conn.autocommit = False
    hooks = list(CONFIGURE)
    for h in hooks:
        h(conn)
    conn.commit()


CONFIGURE: list = []                 # extra per-connection setup (row-level security adds its role here)
BEFORE: list = []                    # fn(raw connection) at the start of every transaction (workspace scope)


def close():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


class PgConn:
    """One transaction on one pooled connection."""

    def __init__(self, conn):
        self.raw = conn

    def execute(self, q: str, args=()) -> Result:
        cur = self.raw.cursor(row_factory=_row_factory)
        cur.execute(_sql(q), tuple(args) if args is not None else ())
        rows = cur.fetchall() if cur.description else []
        return Result(rows, cur.rowcount)

    def executescript(self, script: str):
        for s in statements(script):
            self.raw.execute(s)


@contextmanager
def pg_tx():
    with pool().connection() as conn:
        try:
            for h in BEFORE:
                h(conn)
            yield PgConn(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


class PgAuto:
    """db().execute(...): one statement in its own transaction (like sqlite autocommit)."""

    def execute(self, q: str, args=()) -> Result:
        with pg_tx() as c:
            return c.execute(q, args)

    def executescript(self, script: str):
        with pg_tx() as c:
            c.executescript(script)


def truncate_all():
    """Empty every table of the app (tests only)."""
    with ddl_tx() as c:
        names = [r[0] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema() "
                                         "AND tablename <> 'schema_migrations'")]
        if names:
            c.execute("TRUNCATE " + ", ".join(f'"{n}"' for n in names) + " RESTART IDENTITY CASCADE")


# ------------------------------------------------------------------ workspaces (tenants) and row-level security
#
# Every request, MCP call and job runs in one workspace (TENANT; None = the first workspace, "default"). On
# Postgres each transaction sets app.tenant_id, and row-level security (FORCE, policy tenant_rows) hides other
# workspaces' rows from every table that has a tenant_id column; inserts get the current workspace by default.
# The app works as the role cl_app (no superuser rights, so the policies apply even when the database user is a
# superuser, as in Docker). Lookups that must look across workspaces ("whose key is this?") use system().
import contextvars  # noqa: E402

DEFAULT = "default"
APP_ROLE = "cl_app"
TENANT: contextvars.ContextVar[str | None] = contextvars.ContextVar("cl_tenant", default=None)
BYPASS: contextvars.ContextVar[bool] = contextvars.ContextVar("cl_bypass", default=False)
DDL: contextvars.ContextVar[bool] = contextvars.ContextVar("cl_ddl", default=False)
_role_ok: dict[str, bool] = {}


def current_tenant() -> str:
    return TENANT.get() or DEFAULT


@contextmanager
def tenant_scope(tenant_id: str | None):
    tok = TENANT.set(tenant_id)
    try:
        yield
    finally:
        TENANT.reset(tok)


@contextmanager
def system():
    """Look across workspaces (token lookups, the scheduler). Keep these blocks small."""
    tok = BYPASS.set(True)
    try:
        yield
    finally:
        BYPASS.reset(tok)


def _scope(conn):
    if not DDL.get() and _role_ok.get(_pool_url or ""):
        conn.execute(f"SET LOCAL ROLE {APP_ROLE}")
    conn.execute("SELECT set_config('app.tenant_id', %s, true), set_config('app.bypass', %s, true)",
                 (current_tenant(), "on" if BYPASS.get() else "off"))


BEFORE.append(_scope)

TENANT_COL = ", tenant_id TEXT NOT NULL DEFAULT COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), 'default')"
RLS = """
DO $$
DECLARE t TEXT;
BEGIN
  FOR t IN SELECT c.table_name FROM information_schema.columns c JOIN information_schema.tables x
      ON x.table_name = c.table_name AND x.table_schema = c.table_schema
    WHERE c.table_schema = current_schema() AND c.column_name = 'tenant_id' AND x.table_type = 'BASE TABLE' LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_rows ON %I', t);
    EXECUTE format($p$CREATE POLICY tenant_rows ON %I USING (
        tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on')
      WITH CHECK (tenant_id = current_setting('app.tenant_id', true) OR current_setting('app.bypass', true) = 'on')$p$, t);
  END LOOP;
END $$"""


def ensure_role(c) -> bool:
    """The non-superuser role the app works as, with rights on every table. Managed databases may not allow
    creating roles: then the app works as its own user (row-level security still applies when that user is not a
    superuser, because the policies are FORCEd)."""
    import psycopg
    try:
        with c.raw.transaction():
            if not c.execute("SELECT 1 FROM pg_roles WHERE rolname = ?", (APP_ROLE,)).fetchone():
                c.execute(f"CREATE ROLE {APP_ROLE} NOLOGIN")
            me = c.execute("SELECT current_user").fetchone()[0]
            if me != APP_ROLE:
                c.execute(f'GRANT {APP_ROLE} TO "{me}"')
            c.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
            c.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
            c.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
        ok = True
    except psycopg.Error:
        ok = False
    _role_ok[url()] = ok
    return ok


@contextmanager
def ddl_tx():
    """A transaction for creating tables: as the database user (not cl_app), across workspaces."""
    t1, t2 = DDL.set(True), BYPASS.set(True)
    try:
        with pg_tx() as c:
            yield c
    finally:
        DDL.reset(t1)
        BYPASS.reset(t2)
