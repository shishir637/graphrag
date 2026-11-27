"""The landing zone: raw files as they arrived, behind one small storage interface.

Two areas per workspace, with the folder names every install already has:
  data        tables, one Parquet file (zstd) per table
  documents   original documents (PDF, Word, Markdown, ...)

Every read and write of a table file or an original document goes through `store()`, so another backend (an
S3-compatible object store) can be added later by implementing `Store` and choosing it in `store()`, without
touching any caller. Only `path_for_duckdb()` exposes a location, because DuckDB reads Parquet itself (a local
path today; an s3:// URL with DuckDB's httpfs extension later).

Inside a workspace the areas are CL_FILES_DIR/<workspace>/{data,documents} (rag/wspath.py); on the command line
they are ./data and ./documents, as before.
"""
import os
import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO, Iterable

AREAS = ("data", "documents")


class Store:
    """Interface of a landing-zone backend. `area` is "data" or "documents"; `name` a file name (no folders)."""

    def put(self, area: str, name: str, data: bytes) -> str:
        raise NotImplementedError

    def put_file(self, area: str, name: str, src: str | os.PathLike, move: bool = True) -> str:
        """Land a file written elsewhere (e.g. a Parquet file written in batches)."""
        raise NotImplementedError

    def get(self, area: str, name: str) -> bytes:
        raise NotImplementedError

    def open(self, area: str, name: str, mode: str = "rb") -> BinaryIO:
        raise NotImplementedError

    def list(self, area: str, suffixes: Iterable[str] | None = None) -> list[str]:
        raise NotImplementedError

    def delete(self, area: str, name: str) -> bool:
        raise NotImplementedError

    def exists(self, area: str, name: str) -> bool:
        raise NotImplementedError

    def stat(self, area: str, name: str) -> dict:
        """{"bytes", "modified"} of a stored file."""
        raise NotImplementedError

    def path_for_duckdb(self, area: str, name: str) -> str:
        raise NotImplementedError

    def scratch(self, suffix: str = "") -> str:
        """A temporary local file path next to the landing zone (for batch writers); land it with put_file."""
        raise NotImplementedError


def _area_dir(area: str) -> Path:
    if area == "data":
        from semantic import config
        d = config.DATA_DIR
    elif area == "documents":
        from rag import config
        d = config.DOCS_DIR
    else:
        raise ValueError(f"unknown landing area: {area}")
    return Path(d.resolve_now() if hasattr(d, "resolve_now") else d)


def _check(name: str) -> str:
    if not name or Path(name).name != name or name in (".", ".."):
        raise ValueError(f"not a plain file name: {name!r}")
    return name


class LocalStore(Store):
    """The workspace folders on the local file volume (the existing Docker volumes)."""

    def dir(self, area: str) -> Path:
        return _area_dir(area)

    def _path(self, area: str, name: str) -> Path:
        return self.dir(area) / _check(name)

    def put(self, area: str, name: str, data: bytes) -> str:
        p = self._path(area, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".landing-", dir=p.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, p)                       # readers never see a half-written file
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return str(p)

    def put_file(self, area: str, name: str, src, move: bool = True) -> str:
        p = self._path(area, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        src = Path(src)
        if move and src.parent == p.parent:
            os.replace(src, p)
        else:
            fd, tmp = tempfile.mkstemp(prefix=".landing-", dir=p.parent)
            os.close(fd)
            shutil.copyfile(src, tmp)
            os.replace(tmp, p)
            if move:
                src.unlink(missing_ok=True)
        return str(p)

    def get(self, area: str, name: str) -> bytes:
        return self._path(area, name).read_bytes()

    def open(self, area: str, name: str, mode: str = "rb"):
        if "r" not in mode:
            raise ValueError("open() is for reading; use put() or put_file() to write")
        return open(self._path(area, name), mode)

    def list(self, area: str, suffixes=None) -> list[str]:
        d = self.dir(area)
        if not d.exists():
            return []
        sfx = {s.lower() for s in suffixes} if suffixes else None
        return sorted((p.name for p in d.iterdir()
                       if p.is_file() and not p.name.startswith((".", "_"))
                       and (sfx is None or p.suffix.lower() in sfx)), key=str.lower)

    def delete(self, area: str, name: str) -> bool:
        p = self._path(area, name)
        if p.exists():
            p.unlink()
            return True
        return False

    def exists(self, area: str, name: str) -> bool:
        return self._path(area, name).is_file()

    def stat(self, area: str, name: str) -> dict:
        s = self._path(area, name).stat()
        return {"bytes": s.st_size, "modified": s.st_mtime}

    def path_for_duckdb(self, area: str, name: str) -> str:
        return str(self._path(area, name)).replace("\\", "/")

    def scratch(self, suffix: str = "") -> str:
        d = self.dir("data") / "_tmp"
        d.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="landing-", suffix=suffix, dir=d)
        os.close(fd)
        return tmp


_STORE: Store | None = None


def store() -> Store:
    """The configured landing-zone backend (CL_STORE; only "local" exists today)."""
    global _STORE
    if _STORE is None:
        kind = os.environ.get("CL_STORE", "local")
        if kind != "local":
            raise ValueError(f"CL_STORE={kind}: only the local file store is available in this version.")
        _STORE = LocalStore()
    return _STORE


# ------------------------------------------------------------------ Parquet helpers (tables)

def csv_to_parquet(src: str | os.PathLike, dst: str | os.PathLike):
    """CSV -> Parquet (zstd) with the column names and types DuckDB infers when reading the CSV, which is how the
    tables were read before 0.7, so metrics keep their columns and results."""
    import duckdb
    s, d = str(src).replace("\\", "/"), str(dst).replace("\\", "/")
    con = duckdb.connect()
    try:
        con.execute(f"COPY (SELECT * FROM read_csv_auto('{_q(s)}', header=true)) TO '{_q(d)}' "
                    "(FORMAT parquet, COMPRESSION zstd)")
    finally:
        con.close()


def df_to_parquet(df, dst: str | os.PathLike):
    df.to_parquet(dst, index=False, compression="zstd")


def parquet_info(path: str | os.PathLike) -> dict:
    """{"rows", "schema": [{"name", "type"}]} of a Parquet file, from its footer (no data read)."""
    import pyarrow.parquet as pq
    f = pq.ParquetFile(str(path))
    return {"rows": f.metadata.num_rows,
            "schema": [{"name": fld.name, "type": str(fld.type)} for fld in f.schema_arrow]}


def _q(s: str) -> str:
    return s.replace("'", "''")
