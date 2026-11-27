"""The structured-source family: SQL databases as sources of data tables (connectors/postgres.py, mysql.py).

A SQL connector only says how to talk to its database (connect read-only, list tables, columns and primary keys,
quote names, stream rows with a server-side cursor, map column types to Arrow, attach in DuckDB). Everything else
is shared here:
  snapshot (default)  the chosen tables are copied into the workspace's landing zone as Parquet (zstd) on every
                      sync, read in batches (`batch_rows`, default 50,000) so a table never has to fit in memory.
                      With `updated_column` set, only rows changed since the last sync are read and merged into the
                      stored table by primary key (DuckDB, on the Parquet files). `max_rows` optionally caps a table.
  live                nothing is copied: DuckDB attaches the database read-only (its postgres / mysql extension) and
                      metric SQL runs against it (semantic/db.py), so numbers are always current.
  semantic layer      layer() / access_rules() may return a ready-made semantic layer and row access rules (the
                      default is none: new tables are drafted by the normal data build and an owner approves them).
Use a read-only database user. The connection string is a secret (`dsn`), stored only in app/vault.py.
"""
import datetime as dt
import decimal
import json
import re
import uuid
from pathlib import Path

from .base import Connector, SyncResult, Table

NAME = re.compile(r"[^a-z0-9_]+")
BATCH_ROWS = 50_000
COMMON_CONFIG = {
    "tables": "Tables to read (schema.table or table); default every table in the schemas",
    "mode": "snapshot (copy on each sync, default) or live (query the database directly)",
    "updated_column": "Column with the last-change time, for incremental snapshots (optional)",
    "max_rows": "Most rows copied per table (optional; default no limit)",
    "batch_rows": "Rows read and written per batch (default 50,000)",
}


def clean_name(n: str) -> str:
    n = NAME.sub("_", n.lower()).strip("_")
    return n if n and n[0].isalpha() else f"t_{n}"


def cell(v, typ):
    """A database value as the Arrow type of its column (text for anything unusual)."""
    import pyarrow as pa
    if v is None:
        return None
    if pa.types.is_string(typ):
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False, default=str)
        if isinstance(v, (bytes, bytearray, memoryview)):
            return bytes(v).hex()
        if isinstance(v, (uuid.UUID, dt.time, dt.timedelta, decimal.Decimal)):
            return str(v)
        return str(v)
    if pa.types.is_floating(typ) and isinstance(v, decimal.Decimal):
        return float(v)
    if pa.types.is_integer(typ) and isinstance(v, (decimal.Decimal, bool)):
        return int(v)
    if pa.types.is_boolean(typ) and not isinstance(v, bool):
        return bool(v)
    if pa.types.is_timestamp(typ):
        if isinstance(v, dt.datetime) and typ.tz and v.tzinfo is not None:
            return v.astimezone(dt.timezone.utc)
        if isinstance(v, dt.datetime) and not typ.tz and v.tzinfo is not None:
            return v.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return v


class SqlSource(Connector):
    """Base of SQL database connectors. Subclasses set kind, label, CONFIG, SECRETS, DUCKDB_TYPE and implement the
    methods marked "database-specific"."""
    DUCKDB_TYPE = ""                 # DuckDB extension / ATTACH type for live mode

    # ---------------------------------------------------------------- database-specific
    def connect(self):
        """A DB-API connection, read-only."""
        raise NotImplementedError

    def list_tables(self, c) -> list[tuple[str, str]]:
        """(schema, table) of every table and view the config selects."""
        raise NotImplementedError

    def columns(self, c, schema: str, table: str) -> list[tuple[str, str]]:
        """(column, database type) in order."""
        raise NotImplementedError

    def primary_key(self, c, schema: str, table: str) -> list[str]:
        raise NotImplementedError

    def quote(self, name: str) -> str:
        raise NotImplementedError

    def stream(self, c, query: str, args, batch: int):
        """Yield lists of rows from a server-side cursor."""
        raise NotImplementedError

    def arrow_type(self, db_type: str):
        raise NotImplementedError

    def duckdb_attach(self) -> str:
        """The connection string DuckDB's ATTACH takes for live mode."""
        raise NotImplementedError

    def table_name(self, schema: str, table: str) -> str:
        return clean_name(f"{schema}_{table}")

    # ---------------------------------------------------------------- optional ready-made output
    def layer(self, tables: list[str]) -> dict | None:
        return None

    def access_rules(self, tables: list[str]) -> list[dict]:
        return []

    # ---------------------------------------------------------------- shared
    def _select(self, wanted: list[str] | None, rows: list[tuple[str, str]], default_schema: str):
        if wanted:
            want = {w if "." in w else f"{default_schema}.{w}" for w in wanted}
            rows = [r for r in rows if f"{r[0]}.{r[1]}" in want]
        return rows

    def test(self) -> dict:
        c = self.connect()
        try:
            ts = self.list_tables(c)
        finally:
            c.close()
        if not ts:
            return {"ok": False, "detail": "Connected, but no tables found in the chosen schemas."}
        return {"ok": True, "detail": f"Connected; {len(ts)} tables: " + ", ".join(f"{s}.{t}" for s, t in ts[:20])}

    def snapshot(self, c, query: str, args, cols: list[tuple[str, str]], path: str, upd: str | None):
        """Stream a query into a Parquet file in batches. Returns (rows, highest updated_column value)."""
        import pyarrow as pa
        import pyarrow.parquet as pq
        schema = pa.schema([(n, self.arrow_type(t)) for n, t in cols])
        batch = int(self.config.get("batch_rows") or BATCH_ROWS)
        n, top = 0, None
        ui = [x[0] for x in cols].index(upd) if upd else None
        with pq.ParquetWriter(path, schema, compression="zstd") as w:
            for rows in self.stream(c, query, args, batch):
                arrays = [pa.array([cell(r[i], schema.field(i).type) for r in rows], type=schema.field(i).type)
                          for i in range(len(cols))]
                w.write_table(pa.Table.from_arrays(arrays, schema=schema))
                n += len(rows)
                if ui is not None:
                    vals = [r[ui] for r in rows if r[ui] is not None]
                    if vals:
                        m = max(vals)
                        top = m if top is None or m > top else top
        return n, top

    def sync(self, state: dict) -> SyncResult:
        from app.storage import store
        mode = self.config.get("mode", "snapshot")
        if mode not in ("snapshot", "live"):
            raise ValueError("mode is snapshot or live.")
        res = SyncResult(state=dict(state))
        c = self.connect()
        try:
            tables = self.list_tables(c)
            if mode == "live":
                live = {self.table_name(s, t): f"{s}.{t}" for s, t in tables}
                res.summary = {"mode": "live", "tables": sorted(live),
                               "live_tables_changed": live != (state.get("live_tables") or {})}
                res.state["live_tables"] = live
                res.layer, res.access_rules = self.layer(sorted(live)), self.access_rules(sorted(live))
                return res
            upd = self.config.get("updated_column")
            limit = self.config.get("max_rows")
            lim = f" LIMIT {int(limit)}" if limit else ""
            ph = getattr(self, "PARAM", "%s")
            cursors = dict(state.get("cursors") or {})
            rows_read = {}
            for s, t in tables:
                name = self.table_name(s, t)
                cols = self.columns(c, s, t)
                names = [x[0] for x in cols]
                ident = f"{self.quote(s)}.{self.quote(t)}"
                path = store().scratch(".parquet")
                if upd and upd in names:
                    pk = self.primary_key(c, s, t)
                    since = cursors.get(name)
                    where = f" WHERE {self.quote(upd)} > {ph}" if since else ""
                    q = f"SELECT * FROM {ident}{where} ORDER BY {self.quote(upd)}{lim}"
                    n, top = self.snapshot(c, q, (since,) if since else None, cols, path, upd)
                    if since and pk:
                        if n == 0:
                            Path(path).unlink(missing_ok=True)
                            rows_read[name] = 0
                            continue
                        merge_by_key(name, path, pk)
                    if top is not None:
                        cursors[name] = str(top)
                else:
                    n, _ = self.snapshot(c, f"SELECT * FROM {ident}{lim}", None, cols, path, None)
                rows_read[name] = n
                res.tables.append(Table(name, parquet_path=path))
            res.state["cursors"] = cursors
        finally:
            c.close()
        names = [t.name for t in res.tables]
        res.layer, res.access_rules = self.layer(names), self.access_rules(names)
        res.summary = {"mode": "snapshot", "tables": names, "rows_read": rows_read}
        return res


def merge_by_key(name: str, new_path: str, pk: list[str]):
    """Changed rows (new_path) merged by primary key into the table as stored in the workspace; the result
    replaces new_path. DuckDB does it on the Parquet files, so neither side has to fit in memory."""
    import duckdb
    from app.storage import store
    st = store()
    if not st.exists("data", f"{name}.parquet"):
        return
    old = st.path_for_duckdb("data", f"{name}.parquet")
    new = new_path.replace("\\", "/")
    out = st.scratch(".parquet").replace("\\", "/")
    keys = " AND ".join(f'CAST(n."{k}" AS VARCHAR) = CAST(o."{k}" AS VARCHAR)' for k in pk)
    con = duckdb.connect()
    try:
        con.execute(f"COPY (SELECT * FROM read_parquet('{old}') o WHERE NOT EXISTS "
                    f"(SELECT 1 FROM read_parquet('{new}') n WHERE {keys}) "
                    f"UNION ALL BY NAME SELECT * FROM read_parquet('{new}')) TO '{out}' (FORMAT parquet, COMPRESSION zstd)")
    finally:
        con.close()
    Path(out).replace(new_path)
