"""The app's database: Postgres in production, SQLite for a quick local run and the default test suite.

Postgres when CL_DATABASE_URL (or CL_DATABASE_URL_FILE) is set; a password from CL_DB_PASSWORD (or
CL_DB_PASSWORD_FILE) is put into a URL that has none. Docker Compose sets it, so accounts, sessions, agents,
activity, settings, the access tables (ac_*), teams, the directory, approvals, chats, sources and quality runs all
live in Postgres next to the definitions store (schema cl_defs). Without it, the app keeps using var/app.db.

Both backends take the same SQL: `?` placeholders, sqlite-like rows (row["name"], row[0], dict(row), row.keys()),
ON CONFLICT upserts and RETURNING. Table definitions use {serial} for an auto-numbered id column (INTEGER PRIMARY
KEY in SQLite, BIGSERIAL PRIMARY KEY in Postgres) and {real} for floating point numbers.

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
               .replace("{real}", "DOUBLE PRECISION" if pg else "REAL"))


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
    with pg_tx() as c:
        names = [r[0] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema() "
                                         "AND tablename <> 'schema_migrations'")]
        if names:
            c.execute("TRUNCATE " + ", ".join(f'"{n}"' for n in names) + " RESTART IDENTITY CASCADE")
