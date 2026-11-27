"""Where sources put their files until the landing zone (item 13) exists: scratch files for Parquet written in
batches, and the stored table of a given name in the data folder."""
import secrets
from pathlib import Path


def data_dir() -> Path:
    from semantic import config as SC
    return SC.DATA_DIR


def scratch(suffix: str) -> str:
    d = data_dir() / "_sources_tmp"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / f"{secrets.token_hex(8)}{suffix}")


def table_path(name: str) -> Path | None:
    """The stored table file (Parquet first, then CSV), or None."""
    for ext in (".parquet", ".csv"):
        p = data_dir() / f"{name}{ext}"
        if p.exists():
            return p
    return None


def store_table(name: str, parquet_path: str | None = None, csv: bytes | None = None) -> bool:
    """Put a source's table in the data folder as <name>.parquet (replacing other files of that name).
    Returns False when the content is the same as before."""
    import hashlib
    import duckdb
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    target = d / f"{name}.parquet"
    if parquet_path is None:
        src = scratch(".csv")
        Path(src).write_bytes(csv or b"")
        out = scratch(".parquet")
        con = duckdb.connect()
        try:
            con.execute(f"COPY (SELECT * FROM read_csv_auto('{src.replace(chr(92), '/')}', header=true)) "
                        f"TO '{out.replace(chr(92), '/')}' (FORMAT parquet, COMPRESSION zstd)")
        finally:
            con.close()
        Path(src).unlink(missing_ok=True)
        parquet_path = out

    def digest(p: Path) -> str:
        import duckdb as _d
        c = _d.connect()
        try:
            rows = c.execute(f"SELECT * FROM read_parquet('{str(p).replace(chr(92), '/')}')").fetchall()
        finally:
            c.close()
        return hashlib.sha256(repr(sorted(map(repr, rows))).encode()).hexdigest()
    same = target.exists() and digest(target) == digest(Path(parquet_path))
    if same:
        Path(parquet_path).unlink(missing_ok=True)
        return False
    Path(parquet_path).replace(target)
    for other in d.glob(name + ".*"):
        if other != target and other.suffix.lower() in {".csv", ".xlsx"}:
            other.unlink()
    return True
