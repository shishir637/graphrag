"""DuckDB access to the raw tables: every file in data/ becomes a table with the file's name.

The sample tables are small, so they are read once into memory (fast repeated queries). For large data,
switch CREATE TABLE to CREATE VIEW (reads the files on every query) or point DuckDB at Parquet."""
import duckdb

from .config import DATA_DIR, TABLE_EXTS


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


def connect() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with one table per data file."""
    con = duckdb.connect()
    if any(p.endswith(".xlsx") for p in table_files().values()):
        con.execute("INSTALL excel; LOAD excel;")
    for name, path in table_files().items():
        con.execute(f'CREATE TABLE "{name}" AS SELECT * FROM {_reader(path)}')
    return con


def columns(con, table: str) -> dict[str, str]:
    return {r[0]: r[1] for r in con.execute(f'DESCRIBE "{table}"').fetchall()}
