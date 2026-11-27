"""Postgres for Context Layer: a connection pool, schema migrations, and the workspace (tenant) scope.

Every statement runs inside the current workspace: `app.tenant_id` is set for the transaction, and
row-level security in Postgres hides other workspaces' rows (see migrations/). Code that must look across
workspaces (e.g. "which workspace does this key belong to?") uses `system()`.

The connection runs as the role `cl_app` when it can (a role without superuser rights, so row-level security
applies even when the database user is a superuser, as in the Docker setup).

Env: CL_DATABASE_URL (postgresql://user:pass@host:port/db). Default: the Docker service `db`.
"""
import contextvars
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "migrations"
APP_ROLE = "cl_app"

TENANT: contextvars.ContextVar[str | None] = contextvars.ContextVar("cl_tenant", default=None)
BYPASS: contextvars.ContextVar[bool] = contextvars.ContextVar("cl_bypass", default=False)

_pool = None
_pool_lock = threading.Lock()
_role_ok = False


def url() -> str:
    """CL_DATABASE_URL (or CL_DATABASE_URL_FILE); a password from CL_DB_PASSWORD (or CL_DB_PASSWORD_FILE) is put
    into it when the URL has none."""
    from urllib.parse import quote, urlsplit, urlunsplit
    from .envfile import env
    u = env("CL_DATABASE_URL") or "postgresql://rag@db:5432/contextlayer"
    pw = env("CL_DB_PASSWORD")
    if pw:
        p = urlsplit(u)
        if p.username and p.password is None:
            host = p.hostname + (f":{p.port}" if p.port else "")
            u = urlunsplit((p.scheme, f"{p.username}:{quote(pw, safe='')}@{host}", p.path, p.query, p.fragment))
    return u


# ------------------------------------------------------------------ rows

class Row(tuple):
    """A result row usable like sqlite3.Row: row[0], row["name"], dict(row), row.keys()."""
    __slots__ = ()
    _names: tuple = ()

    def __getitem__(self, k):
        if isinstance(k, str):
            try:
                return tuple.__getitem__(self, self._names.index(k))
            except ValueError:
                raise KeyError(k) from None
        return tuple.__getitem__(self, k)

    def keys(self):
        return list(self._names)

    def get(self, k, default=None):
        return self[k] if k in self._names else default


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
    """Accept sqlite-style ? placeholders; a literal % must be %% for psycopg."""
    return _QMARK.sub("%s", q.replace("%", "%%"))


# ------------------------------------------------------------------ pool and scope

def pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                from psycopg_pool import ConnectionPool
                migrate()
                _pool = ConnectionPool(url(), min_size=1, max_size=int(os.environ.get("CL_DB_POOL", "10")),
                                       configure=_configure, open=True, timeout=30)
    return _pool


def _configure(conn):
    conn.autocommit = False
    if _role_ok:
        conn.execute(f"SET ROLE {APP_ROLE}")
    conn.commit()


def close():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def _scope(conn):
    tenant, bypass = TENANT.get(), BYPASS.get()
    conn.execute("SELECT set_config('app.tenant_id', %s, true), set_config('app.bypass', %s, true)",
                 (tenant or "", "on" if bypass else "off"))


class Conn:
    """One transaction on one pooled connection; `execute` takes ? placeholders like sqlite."""

    def __init__(self, conn):
        self.raw = conn

    def execute(self, q: str, args=()) -> Result:
        cur = self.raw.cursor(row_factory=_row_factory)
        cur.execute(_sql(q), tuple(args) if args is not None else ())
        rows = cur.fetchall() if cur.description else []
        return Result(rows, cur.rowcount)


@contextmanager
def tx():
    """A transaction in the current workspace scope."""
    with pool().connection() as conn:
        try:
            _scope(conn)
            yield Conn(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


class _Auto:
    """db().execute(...) runs one statement in its own transaction (like sqlite autocommit)."""

    def execute(self, q: str, args=()) -> Result:
        with tx() as c:
            return c.execute(q, args)


_auto = _Auto()


def db() -> _Auto:
    return _auto


@contextmanager
def tenant_scope(tenant_id: str | None):
    tok = TENANT.set(tenant_id)
    try:
        yield
    finally:
        TENANT.reset(tok)


@contextmanager
def system():
    """Look across workspaces (token lookups, schedulers). Keep these blocks small."""
    tok = BYPASS.set(True)
    try:
        yield
    finally:
        BYPASS.reset(tok)


def current_tenant() -> str | None:
    return TENANT.get()


# ------------------------------------------------------------------ migrations

def migrate(dsn: str | None = None):
    """Apply migrations/NNN_*.sql once each, in order (as the database owner), and set up the app role."""
    global _role_ok
    import psycopg
    with psycopg.connect(dsn or url(), autocommit=True) as c:
        c.execute("SELECT pg_advisory_lock(727001)")
        try:
            c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied TEXT NOT NULL)")
            done = {r[0] for r in c.execute("SELECT name FROM schema_migrations")}
            for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if f.name in done:
                    continue
                with c.transaction():
                    c.execute(f.read_text(encoding="utf-8"))
                    c.execute("INSERT INTO schema_migrations VALUES (%s, now()::text)", (f.name,))
            _role_ok = _ensure_role(c)
        finally:
            c.execute("SELECT pg_advisory_unlock(727001)")


def _ensure_role(c) -> bool:
    """Create the non-superuser app role and grant it the tables. Managed databases may not allow this:
    then the app runs as its own (non-owner) user and row-level security still applies (FORCE)."""
    import psycopg
    try:
        with c.transaction():
            if not c.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_ROLE,)).fetchone():
                c.execute(f"CREATE ROLE {APP_ROLE} NOLOGIN")
            me = c.execute("SELECT current_user").fetchone()[0]
            c.execute(f'GRANT {APP_ROLE} TO "{me}"')
            c.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
            c.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
            c.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
            c.execute(f"REVOKE ALL ON schema_migrations FROM {APP_ROLE}")
        return True
    except psycopg.Error:
        return False


def reset_for_tests(dsn: str):
    """Point at a fresh test database (tests only)."""
    close()
    os.environ["CL_DATABASE_URL"] = dsn
