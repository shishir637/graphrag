"""Turn uploaded data files into tables DuckDB can query.

  .csv / .parquet   used as they are
  .xlsx / .xls      every non-empty sheet becomes a table: <file> (one sheet) or <file>__<sheet> (several)
  .json             an array of records, an object holding one or more arrays of records, or one record;
                    nested objects are flattened (customer.name -> customer_name); lists of scalars are joined
  .jsonl / .ndjson  one record per line

Converted tables are written as Parquet next to the other tables; the original upload is kept in
data/_uploads/ so it can be re-imported. Column names are cleaned to snake_case so metric definitions
and SQL stay simple.
"""
import json
import re
from pathlib import Path

import pandas as pd

from .config import DATA_DIR

UPLOAD_DIR = DATA_DIR / "_uploads"
ACCEPTED = {".csv", ".parquet", ".xlsx", ".xls", ".json", ".jsonl", ".ndjson"}


def snake(name: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name).strip())
    s = re.sub(r"[^0-9a-zA-Z]+", "_", s).strip("_").lower()
    if not s:
        s = "col"
    return ("c_" + s) if s[0].isdigit() else s


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(how="all").dropna(axis=1, how="all")
    cols, seen = [], {}
    for c in df.columns:
        n = snake(c)
        seen[n] = seen.get(n, 0) + 1
        cols.append(n if seen[n] == 1 else f"{n}_{seen[n]}")
    df.columns = cols
    for c in df.columns:                                   # lists/dicts left after flattening -> text
        if df[c].map(lambda v: isinstance(v, (list, dict))).any():
            df[c] = df[c].map(lambda v: ", ".join(map(str, v)) if isinstance(v, list)
                              else json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v)
        if pd.api.types.is_object_dtype(df[c]) or pd.api.types.is_string_dtype(df[c]):   # ISO dates as text
            sample = df[c].dropna().astype(str).head(50)
            if len(sample) and sample.str.match(r"^\d{4}-\d{2}-\d{2}([T ][\d:.]+Z?)?$").all():
                df[c] = pd.to_datetime(df[c], errors="coerce", utc=False)
        if pd.api.types.is_datetime64_any_dtype(df[c]):    # midnight timestamps -> plain dates
            v = df[c].dropna()
            if len(v) and (v.dt.normalize() == v).all():
                df[c] = df[c].dt.date
    return df


def _records(obj) -> dict[str, list]:
    """Tables found in a JSON document: {table_suffix: records}."""
    if isinstance(obj, list):
        return {"": obj}
    if isinstance(obj, dict):
        lists = {k: v for k, v in obj.items() if isinstance(v, list) and v and all(isinstance(x, dict) for x in v)}
        if lists:
            return lists
        return {"": [obj]}
    raise ValueError("JSON must hold records (objects).")


def _flat(records: list) -> pd.DataFrame:
    return pd.json_normalize(records, sep="_", max_level=3)


def convert(path: Path) -> list[Path]:
    """Convert one uploaded file into table files in DATA_DIR; returns the table files written."""
    ext = path.suffix.lower()
    stem = snake(path.stem)
    out: list[Path] = []
    if ext == ".csv":                                   # kept as CSV (DuckDB reads it directly)
        target = DATA_DIR / f"{stem}.csv"
        target.write_bytes(path.read_bytes())
        out.append(target)
    elif ext == ".parquet":
        target = DATA_DIR / f"{stem}.parquet"
        _clean(pd.read_parquet(path)).to_parquet(target, index=False)
        out.append(target)
    elif ext in (".xlsx", ".xls"):
        sheets = pd.read_excel(path, sheet_name=None)
        sheets = {k: v for k, v in sheets.items() if not v.dropna(how="all").empty}
        for sheet, df in sheets.items():
            name = stem if len(sheets) == 1 else f"{stem}__{snake(sheet)}"
            target = DATA_DIR / f"{name}.parquet"
            _clean(df).to_parquet(target, index=False)
            out.append(target)
    elif ext in (".json", ".jsonl", ".ndjson"):
        text = path.read_text(encoding="utf-8-sig")
        if ext == ".json":
            try:
                tables = _records(json.loads(text))
            except json.JSONDecodeError:           # JSON Lines saved as .json
                tables = {"": [json.loads(line) for line in text.splitlines() if line.strip()]}
        else:
            tables = {"": [json.loads(line) for line in text.splitlines() if line.strip()]}
        for suffix, recs in tables.items():
            name = stem if not suffix or len(tables) == 1 else f"{stem}__{snake(suffix)}"
            target = DATA_DIR / f"{name}.parquet"
            _clean(_flat(recs)).to_parquet(target, index=False)
            out.append(target)
    else:
        raise ValueError(f"{path.name}: use CSV, Excel, JSON, JSON Lines or Parquet.")
    return out


def save_upload(filename: str, data: bytes) -> list[str]:
    """Keep the original in data/_uploads and write the table files. Returns table names."""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    src = UPLOAD_DIR / filename
    src.write_bytes(data)
    try:
        written = convert(src)
    except Exception as e:                              # noqa: BLE001 - shown to the admin
        raise ValueError(f"{filename}: could not read it as a table ({type(e).__name__}: {str(e)[:200]})") from e
    if not written:
        raise ValueError(f"{filename}: no table with data was found.")
    for p in written:                                   # one file per table name
        for other in DATA_DIR.glob(p.stem + ".*"):
            if other != p and other.suffix.lower() in {".csv", ".parquet", ".xlsx"}:
                other.unlink()
    return [p.stem for p in written]
