"""DuckDB access to the raw tables: every file in data/ becomes a view with the file's name (0.9).

Tables land as Parquet (app/landing.py); a view reads the file at query time, so nothing is copied into memory and
large tables work. Files kept as CSV or Excel by earlier versions are read through views too."""
import duckdb

from .config import DATA_DIR, TABLE_EXTS


def table_files() -> dict[str, str]:
    if not DATA_DIR.exists():
        return {}
    return {p.stem: str(p).replace("\\", "/") for p in sorted(DATA_DIR.iterdir())
            if p.suffix.lower() in TABLE_EXTS and not p.name.startswith((".", "_"))}


def _reader(path: str) -> str:
    if path.endswith(".parquet"):
        return f"read_parquet('{path}')"
    if path.endswith(".xlsx"):
        return f"read_xlsx('{path}')"
    return f"read_csv_auto('{path}', header=true)"


def connect() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with one table per data file."""
    con = duckdb.connect()
    if any(p.endswith(".xlsx") for p in table_files().values()):
        con.execute("INSTALL excel; LOAD excel;")
    for name, path in table_files().items():                # views: the files are read at query time, never copied
        con.execute(f'CREATE VIEW "{name}" AS SELECT * FROM {_reader(path)}')
    for s in _live_sources():                               # database sources in live mode: read-only views
        typ = s.get("type", "postgres")
        target = (s.get("attach") or "").replace("'", "''")
        try:
            con.execute(f"INSTALL {typ}; LOAD {typ};")
            con.execute(f"ATTACH '{target}' AS \"{s['alias']}\" (TYPE {typ}, READ_ONLY)")
            for name, src in s["tables"].items():
                schema, table = src.split(".", 1)
                con.execute(f'CREATE OR REPLACE VIEW "{name}" AS SELECT * FROM "{s["alias"]}"."{schema}"."{table}"')
        except duckdb.Error as e:                           # one unreachable database must not stop the others
            print(f"live source {s['alias']} skipped: {e}")
    return con


def _live_sources() -> list[dict]:
    """Database sources in live mode (sources/manage.py); none outside the app."""
    try:
        from sources.manage import live_sources
        return live_sources()
    except Exception:                                      # noqa: BLE001 - no app database here
        return []


def connect_for(scope: dict | None = None, model=None) -> duckdb.DuckDBPyConnection:
    """connect() with only the rows this caller may see (row rules, semantic/access.py). scope: the caller's
    {"groups", "teams", "roles"}; None = no restriction."""
    from .access import restrict
    return restrict(connect(), scope, model)


def connect_scoped(model=None) -> duckdb.DuckDBPyConnection:
    """connect_for the caller of the current MCP call or chat turn (semantic.access.SCOPE)."""
    from .access import SCOPE
    return connect_for(SCOPE.get(), model)


def columns(con, table: str) -> dict[str, str]:
    return {r[0]: r[1] for r in con.execute(f'DESCRIBE "{table}"').fetchall()}
