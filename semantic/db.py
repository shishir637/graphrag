"""DuckDB access to the raw tables: every file in data/ becomes a table with the file's name.

Inside the app there is one in-memory DuckDB per workspace, built from the workspace's files (and, for database
connectors in live mode, views over the attached database), cached and rebuilt only when a file changes.
Each query gets its own cursor; for a caller with row access rules (semantic/access.py) the cursor gets temporary
views that shadow the restricted tables with only the allowed rows, so every metric, join and filter respects
them without changing the SQL compiler.
"""
import os
import threading

import duckdb

from .config import DATA_DIR, TABLE_EXTS

_CACHE: dict[str, tuple] = {}            # data folder -> (signature, base connection)
_LOCK = threading.Lock()
MAX_CACHED = int(os.environ.get("CL_DUCKDB_CACHE", "16"))


def table_files() -> dict[str, str]:
    if not DATA_DIR.exists():                     # a workspace without tables yet
        return {}
    return {p.stem: str(p).replace("\\", "/") for p in sorted(DATA_DIR.iterdir())
            if p.suffix.lower() in TABLE_EXTS}


def _reader(path: str) -> str:
    if path.endswith(".parquet"):
        return f"read_parquet('{path}')"
    if path.endswith(".xlsx"):
        return f"read_xlsx('{path}')"
    return f"read_csv_auto('{path}', header=true)"


def _live_sources() -> list[dict]:
    """Database connectors in live mode: [{"alias", "dsn", "tables": {name: "schema.table"}}]."""
    try:
        from connectors.manage import live_sources
        return live_sources()
    except Exception:                                      # noqa: BLE001 - no app / no connectors
        return []


def _signature(files: dict, live: list) -> tuple:
    return (tuple((n, p, os.path.getmtime(p)) for n, p in files.items()),
            tuple((s["alias"], tuple(sorted(s["tables"].items()))) for s in live))


def _build(files: dict, live: list) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    if any(p.endswith(".xlsx") for p in files.values()):
        con.execute("INSTALL excel; LOAD excel;")
    for name, path in files.items():
        con.execute(f'CREATE TABLE "{name}" AS SELECT * FROM {_reader(path)}')
    for s in live:
        con.execute("INSTALL postgres; LOAD postgres;")
        con.execute(f"ATTACH '{s['dsn']}' AS \"{s['alias']}\" (TYPE postgres, READ_ONLY)")
        for name, src in s["tables"].items():
            schema, table = src.split(".", 1)
            con.execute(f'CREATE VIEW "{name}" AS SELECT * FROM "{s["alias"]}"."{schema}"."{table}"')
    return con


def base() -> duckdb.DuckDBPyConnection:
    """The current workspace's database (cached). Outside the app: a fresh one per call, as before."""
    files, live = table_files(), _live_sources()
    from rag.wspath import current_workspace
    if not current_workspace():
        return _build(files, live)
    key, sig = str(DATA_DIR), _signature(files, live)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] == sig:
            return hit[1]
    con = _build(files, live)
    with _LOCK:
        old = _CACHE.pop(key, None)
        _CACHE[key] = (sig, con)
        while len(_CACHE) > MAX_CACHED:
            _CACHE.pop(next(iter(_CACHE)))[1].close()
    if old:
        try:
            old[1].close()
        except Exception:                                  # noqa: BLE001
            pass
    return con


def connect() -> duckdb.DuckDBPyConnection:
    """A cursor on the workspace's database with one table per data file (no row restrictions)."""
    return base().cursor()


def connect_for(groups: list[str] | None, model=None) -> duckdb.DuckDBPyConnection:
    """A cursor where tables with row access rules show only the rows this caller may see (groups=None: all)."""
    cur = connect()
    from .access import row_filters
    try:
        filters = row_filters(groups)
    except Exception:                                      # noqa: BLE001 - no app: no rules
        filters = None
    if not filters:
        return cur
    if model is None:
        from .yamlio import load_model
        model = load_model()
    tables = {e.name: e.table for e in model.entities}
    db = cur.execute("SELECT current_database()").fetchone()[0]
    for entity, conds in filters.items():
        table = tables.get(entity)
        if not table:
            continue
        if not conds:
            where = "false"                                # rules exist, none for this caller: no rows
        else:
            parts = []
            for col, vals in conds:
                lits = ", ".join("'" + str(v).replace("'", "''") + "'" for v in vals) or "NULL"
                parts.append(f'CAST("{col}" AS VARCHAR) IN ({lits})')
            where = " AND ".join(parts)
        cur.execute(f'CREATE TEMP VIEW "{table}" AS SELECT * FROM "{db}".main."{table}" WHERE {where}')
    return cur


def columns(con, table: str) -> dict[str, str]:
    return {r[0]: r[1] for r in con.execute(f'DESCRIBE "{table}"').fetchall()}
