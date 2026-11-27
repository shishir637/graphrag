"""Landing files in a workspace, and the landing manifest (table landing_files, migrations/011_landing.sql).

Everything raw that arrives goes through here: connector syncs, web uploads, upload links and agents'
add_document. Each landing
  1. writes the file through the storage interface (app/storage.py): tables as Parquet (zstd) in data/,
     originals in documents/
  2. writes one manifest row: who pushed it (source, connector, user or agent), groups, sha256, size, and for
     tables the row count and schema read from the Parquet footer. The previous row of the same file gets
     replaced_at, so the manifest keeps the full history.
Landing an identical file again changes nothing (same sha256), so syncs that bring no change start no rebuild.

Outside a workspace (command line, research mode) files are written the same way and no manifest is kept.

backfill() brings a workspace created before 0.7 to this layout: CSV tables converted to Parquet with the types
DuckDB inferred from them (the original CSV kept in data/_uploads/), and a manifest row for every file already
there. It is idempotent: a second run changes nothing.
"""
import hashlib
import json
from pathlib import Path

from .storage import csv_to_parquet, parquet_info, store

TABLE_SUFFIXES = {".parquet"}
OLD_TABLE_SUFFIXES = {".csv", ".xlsx"}


def _tenant() -> str | None:
    from .db import current_tenant
    return current_tenant()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def current(kind: str | None = None) -> list[dict]:
    """Current manifest rows of the workspace (replaced files left out)."""
    t = _tenant()
    if not t:
        return []
    from .db import db
    q = "SELECT * FROM landing_files WHERE tenant_id = ? AND replaced_at IS NULL"
    args: tuple = (t,)
    if kind:
        q += " AND kind = ?"
        args += (kind,)
    return [dict(r) for r in db().execute(q + " ORDER BY kind, name", args)]


def history(name: str, kind: str = "table") -> list[dict]:
    t = _tenant()
    if not t:
        return []
    from .db import db
    return [dict(r) for r in db().execute("SELECT * FROM landing_files WHERE tenant_id = ? AND kind = ? AND name = ? "
                                          "ORDER BY id", (t, kind, name))]


def _current_sha(kind: str, name: str) -> str | None:
    t = _tenant()
    if not t:
        return None
    from .db import db
    r = db().execute("SELECT sha256 FROM landing_files WHERE tenant_id = ? AND kind = ? AND name = ? AND replaced_at IS NULL "
                     "ORDER BY id DESC LIMIT 1", (t, kind, name)).fetchone()
    return r["sha256"] if r else None


def record(kind: str, name: str, path: str, sha: str, size: int, source: str = "upload", connector_id: str | None = None,
           pushed_by: str = "", groups: list[str] | None = None, rows: int | None = None, schema=None) -> bool:
    """One manifest row (the previous row of the same file marked replaced). False when nothing changed."""
    t = _tenant()
    if not t:
        return True
    from .core import now
    from .db import db
    if _current_sha(kind, name) == sha:
        return False
    ts = now()
    db().execute("UPDATE landing_files SET replaced_at = ? WHERE tenant_id = ? AND kind = ? AND name = ? AND replaced_at IS NULL",
                 (ts, t, kind, name))
    db().execute("INSERT INTO landing_files (tenant_id, kind, name, path, source, connector_id, pushed_by, groups, sha256, bytes, "
                 "rows, schema, landed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?::jsonb, ?, ?, ?, ?::jsonb, ?)",
                 (t, kind, name, path, source, connector_id, pushed_by or "", json.dumps(groups or []), sha, size, rows,
                  json.dumps(schema) if schema is not None else None, ts))
    return True


def removed(kind: str, name: str):
    """A file left the landing zone: its current manifest row gets replaced_at (the row itself is kept)."""
    t = _tenant()
    if not t:
        return
    from .core import now
    from .db import db
    db().execute("UPDATE landing_files SET replaced_at = ? WHERE tenant_id = ? AND kind = ? AND name = ? AND replaced_at IS NULL",
                 (now(), t, kind, name))


# ------------------------------------------------------------------ tables

def _drop_other_formats(table: str):
    """One file per table name: an older CSV / Excel copy of the same table is removed (kept in data/_uploads)."""
    st = store()
    for ext in OLD_TABLE_SUFFIXES:
        name = f"{table}{ext}"
        if st.exists("data", name):
            _keep_original(name)


def _keep_original(name: str):
    """Move data/<name> to data/_uploads/<name> (the originals folder), never overwriting a different file."""
    from .storage import store
    src = store().dir("data") / name
    up = src.parent / "_uploads"
    up.mkdir(parents=True, exist_ok=True)
    dst = up / name
    if dst.exists():
        if sha256_file(dst) == sha256_file(src):
            src.unlink()
            return
        dst = up / f"{src.stem}.pre-0.7{src.suffix}"
        n = 2
        while dst.exists():
            dst, n = up / f"{src.stem}.pre-0.7-{n}{src.suffix}", n + 1
    src.replace(dst)


def land_table_file(table: str, src, source: str = "upload", connector_id: str | None = None, pushed_by: str = "",
                    groups: list[str] | None = None) -> bool:
    """Land a Parquet file written elsewhere (moved into data/<table>.parquet). True when the table changed."""
    st = store()
    name = f"{table}.parquet"
    sha = sha256_file(src)
    known = _current_sha("table", name)
    if known is None and st.exists("data", name):        # outside a workspace / not yet in the manifest
        with st.open("data", name) as f:
            known = hashlib.sha256(f.read()).hexdigest()
        if known == sha and not _tenant():
            Path(src).unlink(missing_ok=True)
            return False
    if known == sha and st.exists("data", name):
        Path(src).unlink(missing_ok=True)
        _drop_other_formats(table)
        return False
    info = parquet_info(src)
    size = Path(src).stat().st_size
    path = st.put_file("data", name, src)
    _drop_other_formats(table)
    record("table", name, path, sha, size, source, connector_id, pushed_by, groups, info["rows"], info["schema"])
    return True


def land_table_csv(table: str, data: bytes, **kw) -> bool:
    """A table given as CSV bytes (older connectors): converted to Parquet with DuckDB's inferred types."""
    st = store()
    src = st.scratch(".csv")
    dst = st.scratch(".parquet")
    try:
        Path(src).write_bytes(data)
        csv_to_parquet(src, dst)
    finally:
        Path(src).unlink(missing_ok=True)
    return land_table_file(table, dst, **kw)


def land_table_df(table: str, df, **kw) -> bool:
    st = store()
    dst = st.scratch(".parquet")
    df.to_parquet(dst, index=False, compression="zstd")
    return land_table_file(table, dst, **kw)


def remove_table(table: str):
    st = store()
    for ext in TABLE_SUFFIXES | OLD_TABLE_SUFFIXES:
        if st.delete("data", f"{table}{ext}"):
            removed("table", f"{table}{ext}")


# ------------------------------------------------------------------ documents

def land_document(name: str, data: bytes, groups: list[str] | None = None, source: str = "upload",
                  connector_id: str | None = None, pushed_by: str = "") -> bool:
    """Write an original document and its manifest row. True when it is new or changed."""
    st = store()
    sha = hashlib.sha256(data).hexdigest()
    if st.exists("documents", name) and _current_sha("document", name) == sha:
        return False
    path = st.put("documents", name, data)
    record("document", name, path, sha, len(data), source, connector_id, pushed_by, groups)
    return True


def remove_document(name: str) -> bool:
    gone = store().delete("documents", name)
    removed("document", name)
    return gone


# ------------------------------------------------------------------ audit archive (optional)

def archive_events(rows: list[dict]) -> dict[str, int]:
    """Old audit events copied to <workspace>/archive/events-YYYY-MM.parquet before retention deletes them; a month
    already archived is merged (by event id), so running it twice changes nothing. Returns rows per file."""
    import duckdb
    import pandas as pd
    if not rows:
        return {}
    from rag.wspath import current_workspace, files_root
    t = current_workspace()
    d = (files_root() / t / "archive") if t else (Path(__file__).resolve().parent.parent / "var" / "archive")
    d.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    df["month"] = df["time"].str[:7]
    out = {}
    for month, part in df.groupby("month"):
        path = d / f"events-{month}.parquet"
        part = part.drop(columns=["month"])
        if path.exists():
            p = str(path).replace("\\", "/")
            old = duckdb.connect().execute(f"SELECT * FROM read_parquet('{p}')").df()
            part = pd.concat([old, part]).drop_duplicates(subset=["id"], keep="last").sort_values("id")
        tmp = path.with_suffix(".tmp")
        part.to_parquet(tmp, index=False, compression="zstd")
        tmp.replace(path)
        out[path.name] = len(part)
    return out


# ------------------------------------------------------------------ 0.7 backfill

def backfill() -> dict:
    """Bring the current workspace's landing zone to the 0.7 layout. Idempotent."""
    st = store()
    out = {"tables_converted": 0, "manifest_rows": 0}
    for name in st.list("data", {".csv"}):               # CSV tables -> Parquet (same column types as before)
        table = Path(name).stem
        target = f"{table}.parquet"
        if not st.exists("data", target):
            tmp = st.scratch(".parquet")
            csv_to_parquet(st.path_for_duckdb("data", name), tmp)
            st.put_file("data", target, tmp)
            out["tables_converted"] += 1
        _keep_original(name)
    if not _tenant():
        return out
    have = {(r["kind"], r["name"]) for r in current()}
    for name in st.list("data", TABLE_SUFFIXES | {".xlsx"}):
        if ("table", name) in have:
            continue
        path = st.path_for_duckdb("data", name)
        info = parquet_info(path) if name.endswith(".parquet") else {"rows": None, "schema": None}
        record("table", name, path, sha256_file(path), st.stat("data", name)["bytes"], "backfill",
               rows=info["rows"], schema=info["schema"])
        out["manifest_rows"] += 1
    from rag.registry import access_map, sources
    groups, srcs = access_map(), sources()
    for name in st.list("documents"):
        if ("document", name) in have:
            continue
        path = st.path_for_duckdb("documents", name)
        src = (srcs.get(name) or {}).get("source", "upload")
        cid = src.split(":", 1)[1] if src.startswith("connector:") else None
        record("document", name, path, sha256_file(path), st.stat("documents", name)["bytes"],
               "connector" if cid else "backfill", cid, groups=groups.get(name, ["public"]))
        out["manifest_rows"] += 1
    return out
