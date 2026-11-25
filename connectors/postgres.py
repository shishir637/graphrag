"""A Postgres database (e.g. the company's reporting database or warehouse replica) as a source of data tables.

Two modes:
  snapshot (default)  the chosen tables are copied (CSV) into the workspace on every sync. With
                      `updated_column` set, only rows changed since the last sync are read and merged by primary key.
  live                nothing is copied: DuckDB attaches the database read-only and metric SQL runs against it
                      (semantic/db.py), so numbers are always current. Needs the DuckDB postgres extension.
New tables get drafted definitions (entities, joins, count/total/average metrics) through the normal data build;
an owner approves the metrics. Use a read-only database user.

Config: schemas (["public"]), tables (optional list of schema.table or table names; default: all in the schemas),
mode (snapshot | live), updated_column (optional, e.g. "updated_at"), max_rows (default 2,000,000 per table).
Secret: dsn (postgresql://user:password@host:5432/database).
"""
import csv
import io
import re

from .base import Connector, SyncResult, Table

NAME = re.compile(r"[^a-z0-9_]+")


def table_name(schema: str, table: str) -> str:
    n = NAME.sub("_", (table if schema == "public" else f"{schema}_{table}").lower()).strip("_")
    return n if n and n[0].isalpha() else f"t_{n}"


class Postgres(Connector):
    kind = "postgres"
    label = "Postgres database"
    CONFIG = {"schemas": "Schemas to read (default [\"public\"])",
              "tables": "Tables to read (schema.table or table); default every table in the schemas",
              "mode": "snapshot (copy on each sync, default) or live (query the database directly)",
              "updated_column": "Column with the last-change time, for incremental snapshots (optional)",
              "max_rows": "Most rows copied per table (default 2,000,000)"}
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

    def test(self) -> dict:
        with self._conn() as c:
            ts = self._tables(c)
        if not ts:
            return {"ok": False, "detail": "Connected, but no tables found in the chosen schemas."}
        return {"ok": True, "detail": f"Connected; {len(ts)} tables: " + ", ".join(f"{s}.{t}" for s, t in ts[:20])}

    def sync(self, state: dict) -> SyncResult:
        from psycopg import sql
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
            limit = int(self.config.get("max_rows", 2_000_000))
            cursors = dict(state.get("cursors") or {})
            for s, t in tables:
                name = table_name(s, t)
                cols = [r[0] for r in c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = %s "
                                                "AND table_name = %s ORDER BY ordinal_position", (s, t))]
                ident = sql.Identifier(s, t)
                if upd and upd in cols:
                    pk = [r[0] for r in c.execute(
                        "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
                        "AND a.attnum = ANY(i.indkey) WHERE i.indrelid = %s::regclass AND i.indisprimary",
                        (f'"{s}"."{t}"',))]
                    since = cursors.get(name)
                    q = sql.SQL("SELECT * FROM {} {} ORDER BY {} LIMIT {}").format(
                        ident, sql.SQL("WHERE {} > %s").format(sql.Identifier(upd)) if since else sql.SQL(""),
                        sql.Identifier(upd), sql.Literal(limit))
                    cur = c.execute(q, (since,) if since else None)
                    new = cur.fetchall()
                    data = _merge(name, cols, new, pk) if since and pk else _csv(cols, new)
                    if new:
                        cursors[name] = str(max(r[cols.index(upd)] for r in new if r[cols.index(upd)] is not None))
                else:
                    buf = io.BytesIO()
                    copy_q = sql.SQL("COPY (SELECT * FROM {} LIMIT {}) TO STDOUT WITH (FORMAT csv, HEADER true)").format(
                        ident, sql.Literal(limit))
                    with c.cursor().copy(copy_q) as cp:
                        for block in cp:
                            buf.write(bytes(block))
                    data = buf.getvalue()
                res.tables.append(Table(name, data))
            res.state["cursors"] = cursors
        res.summary = {"mode": "snapshot", "tables": [t.name for t in res.tables]}
        return res


def _csv(cols, rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    w.writerows(["" if v is None else v for v in r] for r in rows)
    return buf.getvalue().encode("utf-8")


def _merge(name, cols, new, pk) -> bytes:
    """The table as stored in the workspace, with changed rows replaced (by primary key)."""
    from semantic.config import DATA_DIR
    path = DATA_DIR / f"{name}.csv"
    rows: dict[tuple, list] = {}
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[tuple(r[k] for k in pk)] = [r.get(cname, "") for cname in cols]
    for r in new:
        d = dict(zip(cols, r))
        rows[tuple(str(d[k]) for k in pk)] = ["" if v is None else v for v in r]
    return _csv(cols, rows.values())
