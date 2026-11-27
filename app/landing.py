"""The landing zone for data tables (0.9): every table that arrives (an upload, a source sync) is stored as one
Parquet file (zstd) in the workspace's data folder, written atomically, and recorded in the manifest
(data/_manifest.json): where it came from, who pushed it, when, rows, columns and a content hash. DuckDB reads the
files directly through views (semantic/db.py), so nothing is copied into memory and a table that is being replaced
is never half-read. Ported in spirit from dev app/landing.py (that one also kept documents; here documents stay in
the documents folder).

backfill() converts tables kept as CSV or Excel by earlier versions to Parquet (the original goes to
data/_uploads/). Docker runs it at start (CL_LANDING_BACKFILL=1); python -m app.landing backfill does the same.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

MANIFEST = "_manifest.json"


def _data() -> Path:
    from semantic import config as SC
    return Path(SC.DATA_DIR)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def manifest(folder: Path | None = None) -> dict:
    p = Path(folder or _data()) / MANIFEST
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_manifest(m: dict, folder: Path | None = None):
    d = Path(folder or _data())
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, d / MANIFEST)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _shape(path: Path) -> tuple[int, list[str]]:
    import duckdb
    con = duckdb.connect()
    try:
        p = str(path).replace("\\", "/")
        rows = con.execute(f"SELECT count(*) FROM read_parquet('{p}')").fetchone()[0]
        cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{p}')").fetchall()]
    finally:
        con.close()
    return rows, cols


def csv_to_parquet(src: Path, target: Path):
    """A CSV file as Parquet, with the column types DuckDB infers (the same ones queries saw before)."""
    import duckdb
    con = duckdb.connect()
    try:
        con.execute(f"COPY (SELECT * FROM read_csv_auto('{str(src).replace(chr(92), '/')}', header=true)) "
                    f"TO '{str(target).replace(chr(92), '/')}' (FORMAT parquet, COMPRESSION zstd)")
    finally:
        con.close()


def land(name: str, parquet_path: Path | str, source: str = "upload", by: str = "", original: str = "") -> dict:
    """Move a Parquet file into the data folder as <name>.parquet (replacing the table) and record it."""
    d = _data()
    d.mkdir(parents=True, exist_ok=True)
    target = d / f"{name}.parquet"
    tmp = d / f".{name}.landing.parquet"
    Path(parquet_path).replace(tmp)
    os.replace(tmp, target)                              # atomic: queries see the old table or the new one
    for other in d.glob(name + ".*"):
        if other != target and other.suffix.lower() in {".csv", ".xlsx"}:
            other.unlink()
    return record(target, source, by, original)


def record(path: Path, source: str = "upload", by: str = "", original: str = "") -> dict:
    """Add (or refresh) a table file's manifest entry."""
    path = Path(path)
    rows, cols = _shape(path) if path.suffix == ".parquet" else (None, [])
    entry = {"file": path.name, "format": path.suffix.lstrip("."), "rows": rows, "columns": cols,
             "sha256": _sha256(path), "source": source, "pushed_by": by, "original": original or path.name,
             "landed": _now()}
    m = manifest(path.parent)                            # the manifest sits next to the table files
    m[path.stem] = entry
    _save_manifest(m, path.parent)
    return entry


def forget(name: str):
    m = manifest()
    if m.pop(name, None) is not None:
        _save_manifest(m)


def backfill() -> dict:
    """Tables kept as CSV or Excel by earlier versions converted to Parquet (originals kept in data/_uploads)."""
    from semantic.ingest import UPLOAD_DIR, convert
    d = _data()
    if not d.exists():
        return {"converted": []}
    done = []
    for p in sorted(d.iterdir()):
        if p.suffix.lower() not in {".csv", ".xlsx"} or p.name.startswith("_") or (d / f"{p.stem}.parquet").exists():
            continue
        Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
        keep = Path(UPLOAD_DIR) / p.name
        if not keep.exists():
            keep.write_bytes(p.read_bytes())
        for t in convert(keep):
            if t.suffix == ".parquet":
                record(t, "backfill", "upgrade", p.name)
        if (d / f"{p.stem}.parquet").exists():
            p.unlink()
        done.append(p.name)
    return {"converted": done}


if __name__ == "__main__":
    if sys.argv[1:] == ["backfill"]:
        print(backfill())
    else:
        raise SystemExit("usage: python -m app.landing backfill")
