"""A Postgres database (e.g. the company's reporting database or warehouse replica) as a source of data tables.

Built on the SQL connector base (sources/sql_base.py: snapshot to Parquet in batches, incremental merge by
primary key, live mode through DuckDB's postgres extension). This module only says how to talk to Postgres:
psycopg, a read-only session, information_schema, a named (server-side) cursor, and the Postgres -> Arrow types.

Config: schemas (["public"]), tables, mode (snapshot | live), updated_column, max_rows, batch_rows.
Secret: dsn (postgresql://user:password@host:5432/database). Use a read-only database user.
"""
from .sql_base import COMMON_CONFIG, SqlSource, clean_name


def table_name(schema: str, table: str) -> str:
    return clean_name(table if schema == "public" else f"{schema}_{table}")


class Postgres(SqlSource):
    kind = "postgres"
    label = "Postgres database"
    DUCKDB_TYPE = "postgres"
    CONFIG = {"schemas": "Schemas to read (default [\"public\"])"} | COMMON_CONFIG
    SECRETS = {"dsn": "postgresql://user:password@host:5432/database (use a read-only user)"}

    def connect(self):
        import psycopg
        dsn = self.secrets.get("dsn")
        if not dsn:
            raise ValueError("Set the connection string (dsn) secret.")
        c = psycopg.connect(dsn, connect_timeout=15, autocommit=True)
        c.execute("SET default_transaction_read_only = on")
        return c

    def list_tables(self, c) -> list[tuple[str, str]]:
        schemas = self.config.get("schemas") or ["public"]
        rows = c.execute("SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema = ANY(%s) "
                         "AND table_type IN ('BASE TABLE', 'VIEW') ORDER BY 1, 2", (list(schemas),)).fetchall()
        return self._select(self.config.get("tables"), [(r[0], r[1]) for r in rows], schemas[0])

    def columns(self, c, schema: str, table: str) -> list[tuple[str, str]]:
        return [(r[0], r[1]) for r in c.execute(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = %s AND table_name = %s "
            "ORDER BY ordinal_position", (schema, table))]

    def primary_key(self, c, schema: str, table: str) -> list[str]:
        return [r[0] for r in c.execute(
            "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "AND a.attnum = ANY(i.indkey) WHERE i.indrelid = %s::regclass AND i.indisprimary", (f'"{schema}"."{table}"',))]

    def quote(self, name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def stream(self, c, query: str, args, batch: int):
        with c.transaction(), c.cursor(name="cl_snapshot") as cur:       # server-side cursor: rows arrive in batches
            cur.itersize = batch
            cur.execute(query, args)
            while True:
                rows = cur.fetchmany(batch)
                if not rows:
                    return
                yield rows

    def arrow_type(self, db_type: str):
        import pyarrow as pa
        t = db_type.lower()
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

    def duckdb_attach(self) -> str:
        return self.secrets.get("dsn") or ""

    def table_name(self, schema: str, table: str) -> str:
        return table_name(schema, table)
