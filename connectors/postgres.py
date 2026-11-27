"""A Postgres database (e.g. the company's reporting database or warehouse replica) as a source of data tables.

Two modes:
  snapshot (default)  the chosen tables are copied into the workspace's landing zone as Parquet (zstd) on every
                      sync. Rows are read with a server-side cursor and written in batches (pyarrow), so a table
                      never has to fit in memory. With `updated_column` set, only rows changed since the last sync
                      are read and merged into the stored table by primary key (DuckDB, on the Parquet files).
  live                nothing is copied: DuckDB attaches the database read-only and metric SQL runs against it
                      (semantic/db.py), so numbers are always current. Needs the DuckDB postgres extension.
New tables get drafted definitions (entities, joins, count/total/average metrics) through the normal data build;
an owner approves the metrics. Use a read-only database user.

Config: schemas (["public"]), tables (optional list of schema.table or table names; default: all in the schemas),
mode (snapshot | live), updated_column (optional, e.g. "updated_at"), max_rows (optional cap per table; default
none), batch_rows (rows per Parquet batch, default 50,000).
Secret: dsn (postgresql://user:password@host:5432/database).
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


def table_name(schema: str, table: str) -> str:
    n = NAME.sub("_", (table if schema == "public" else f"{schema}_{table}").lower()).strip("_")
    return n if n and n[0].isalpha() else f"t_{n}"


# Postgres type name -> Arrow type (anything else is stored as text)
def _arrow_type(pg_type: str):
    import pyarrow as pa
    t = pg_type.lower()
    if t in ("smallint", "int2"):
        return pa.int16()
    if t in ("integer", "int", "int4"):
        return pa.int32()
    if t in ("bigint", "int8"):
        return pa.int64()
    if t in ("real", "float4"):
        return pa.float32()
    if t in ("double precision", "float8", "numeric", "decimal", "money"):
        return pa.float64()
    if t in ("boolean", "bool"):
        return pa.bool_()
    if t == "date":
        return pa.date32()
    if t.startswith("timestamp") and "with time zone" in t:
        return pa.timestamp("us", tz="UTC")
    if t.startswith("timestamp"):
        return pa.timestamp("us")
    return pa.string()


def _cell(v, typ):
    import pyarrow as pa
    if v is None:
        return None
    if pa.types.is_string(typ):
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False, default=str)
        if isinstance(v, (uuid.UUID, dt.time, dt.timedelta)):
            return str(v)
        if isinstance(v, (bytes, memoryview)):
            return bytes(v).hex()
        return str(v)
    if pa.types.is_floating(typ) and isinstance(v, decimal.Decimal):
        return float(v)
    if pa.types.is_timestamp(typ) and typ.tz and isinstance(v, dt.datetime) and v.tzinfo is not None:
        return v.astimezone(dt.timezone.utc)
    return v


class Postgres(Connector):
    kind = "postgres"
    label = "Postgres database"
    CONFIG = {"schemas": "Schemas to read (default [\"public\"])",
              "tables": "Tables to read (schema.table or table); default every table in the schemas",
              "mode": "snapshot (copy on each sync, default) or live (query the database directly)",
              "updated_column": "Column with the last-change time, for incremental snapshots (optional)",
              "max_rows": "Most rows copied per table (optional; default no limit)",
              "batch_rows": "Rows read and written per batch (default 50,000)"}
    SECRETS = {"dsn": "postgresql://user:password@host:5432/database (use a read-only user)"}

    def _conn(self):
        import psycopg
        dsn = self.secrets.get("dsn")
        if not dsn:
            raise ValueError("Set the connection string (dsn) secret.")
        c = psycopg.connect(dsn, connect_timeout=15, autocommit=True)
        c.execute("SET default_transaction_read_only = on")
        return c

    def _tables(self, c) -> list[tuple[str, str]]:
        schemas = self.config.get("schemas") or ["public"]
        rows = c.execute("SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema = ANY(%s) "
                         "AND table_type IN ('BASE TABLE', 'VIEW') ORDER BY 1, 2", (list(schemas),)).fetchall()
        want = self.config.get("tables")
        if want:
            want = {w if "." in w else f"{schemas[0]}.{w}" for w in want}
            rows = [r for r in rows if f"{r[0]}.{r[1]}" in want]
        return [(r[0], r[1]) for r in rows]

    def _columns(self, c, s: str, t: str) -> list[tuple[str, str]]:
        return [(r[0], r[1]) for r in c.execute(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = %s AND table_name = %s "
            "ORDER BY ordinal_position", (s, t))]

    def _pk(self, c, s: str, t: str) -> list[str]:
        return [r[0] for r in c.execute(
            "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "AND a.attnum = ANY(i.indkey) WHERE i.indrelid = %s::regclass AND i.indisprimary", (f'"{s}"."{t}"',))]

    def test(self) -> dict:
        with self._conn() as c:
            ts = self._tables(c)
        if not ts:
            return {"ok": False, "detail": "Connected, but no tables found in the chosen schemas."}
        return {"ok": True, "detail": f"Connected; {len(ts)} tables: " + ", ".join(f"{s}.{t}" for s, t in ts[:20])}

    def _snapshot(self, c, query, args, cols: list[tuple[str, str]], path: str, upd: str | None) -> tuple[int, object]:
        """Stream a query into a Parquet file in batches. Returns (rows, highest updated_column value)."""
        import pyarrow as pa
        import pyarrow.parquet as pq
        schema = pa.schema([(n, _arrow_type(t)) for n, t in cols])
        batch = int(self.config.get("batch_rows") or BATCH_ROWS)
        n, top = 0, None
        ui = [x[0] for x in cols].index(upd) if upd else None
        with pq.ParquetWriter(path, schema, compression="zstd") as w, c.transaction():
            with c.cursor(name="cl_snapshot") as cur:       # server-side cursor: rows arrive in batches
                cur.itersize = batch
                cur.execute(query, args)
                while True:
                    rows = cur.fetchmany(batch)
                    if not rows:
                        break
                    data = [[_cell(r[i], schema.field(i).type) for r in rows] for i in range(len(cols))]
                    w.write_table(pa.Table.from_arrays([pa.array(col, type=schema.field(i).type)
                                                        for i, col in enumerate(data)], schema=schema))
                    n += len(rows)
                    if ui is not None:
                        vals = [r[ui] for r in rows if r[ui] is not None]
                        if vals:
                            m = max(vals)
                            top = m if top is None or m > top else top
        return n, top

    def sync(self, state: dict) -> SyncResult:
        from psycopg import sql
        from app.storage import store
        mode = self.config.get("mode", "snapshot")
        if mode not in ("snapshot", "live"):
            raise ValueError("mode is snapshot or live.")
        res = SyncResult(state=dict(state))
        with self._conn() as c:
            tables = self._tables(c)
            if mode == "live":
                live = {table_name(s, t): f"{s}.{t}" for s, t in tables}
                res.summary = {"mode": "live", "tables": sorted(live),
                               "live_tables_changed": live != (state.get("live_tables") or {})}
                res.state["live_tables"] = live
                return res
            upd = self.config.get("updated_column")
            limit = self.config.get("max_rows")
            cursors = dict(state.get("cursors") or {})
            rows_read = {}
            for s, t in tables:
                name = table_name(s, t)
                cols = self._columns(c, s, t)
                names = [x[0] for x in cols]
                ident = sql.Identifier(s, t)
                lim = sql.SQL(" LIMIT {}").format(sql.Literal(int(limit))) if limit else sql.SQL("")
                path = store().scratch(".parquet")
                if upd and upd in names:
                    pk = self._pk(c, s, t)
                    since = cursors.get(name)
                    q = sql.SQL("SELECT * FROM {} {} ORDER BY {}{}").format(
                        ident, sql.SQL("WHERE {} > %s").format(sql.Identifier(upd)) if since else sql.SQL(""),
                        sql.Identifier(upd), lim)
                    n, top = self._snapshot(c, q, (since,) if since else None, cols, path, upd)
                    if since and pk:
                        if n == 0:
                            Path(path).unlink(missing_ok=True)
                            rows_read[name] = 0
                            continue
                        _merge(name, path, pk)
                    if top is not None:
                        cursors[name] = str(top)
                else:
                    q = sql.SQL("SELECT * FROM {}{}").format(ident, lim)
                    n, _ = self._snapshot(c, q, None, cols, path, None)
                rows_read[name] = n
                res.tables.append(Table(name, parquet_path=path))
            res.state["cursors"] = cursors
        res.summary = {"mode": "snapshot", "tables": [t.name for t in res.tables], "rows_read": rows_read}
        return res


def _merge(name: str, new_path: str, pk: list[str]):
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
