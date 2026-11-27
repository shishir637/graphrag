"""A MySQL / MariaDB database as a source of data tables: the second implementation of the SQL connector base
(sources/sql_base.py), which proves the base: snapshot to Parquet in batches, incremental merge by primary key,
live mode through DuckDB's mysql extension, all shared.

The driver (PyMySQL) is an optional dependency: pip install pymysql. Without it the connector is not offered.

Config: databases (default: the database in the connection string), tables, mode (snapshot | live),
updated_column, max_rows, batch_rows.
Secret: dsn (mysql://user:password@host:3306/database). Use a read-only database user.
"""
from urllib.parse import unquote, urlparse

import pymysql                                       # optional dependency: the connector is skipped without it
import pymysql.cursors

from .sql_base import COMMON_CONFIG, SqlSource, clean_name


def parse_dsn(dsn: str) -> dict:
    u = urlparse(dsn)
    if u.scheme not in ("mysql", "mariadb") or not u.hostname:
        raise ValueError("The connection string looks like mysql://user:password@host:3306/database.")
    return {"host": u.hostname, "port": u.port or 3306, "user": unquote(u.username or ""),
            "password": unquote(u.password or ""), "database": (u.path or "/").lstrip("/")}


class MySQL(SqlSource):
    kind = "mysql"
    label = "MySQL / MariaDB database"
    DUCKDB_TYPE = "mysql"
    CONFIG = {"databases": "Databases (schemas) to read (default: the one in the connection string)"} | COMMON_CONFIG
    SECRETS = {"dsn": "mysql://user:password@host:3306/database (use a read-only user)"}

    def _dsn(self) -> dict:
        dsn = self.secrets.get("dsn")
        if not dsn:
            raise ValueError("Set the connection string (dsn) secret.")
        return parse_dsn(dsn)

    def _databases(self) -> list[str]:
        return self.config.get("databases") or [self._dsn()["database"]]

    def connect(self):
        d = self._dsn()
        c = pymysql.connect(host=d["host"], port=d["port"], user=d["user"], password=d["password"],
                            database=d["database"] or None, connect_timeout=15, charset="utf8mb4", autocommit=True)
        with c.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
        return c

    def _q(self, c, sql: str, args=None) -> list[tuple]:
        with c.cursor() as cur:
            cur.execute(sql, args)
            return list(cur.fetchall())

    def list_tables(self, c) -> list[tuple[str, str]]:
        dbs = self._databases()
        rows = self._q(c, "SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema IN %s "
                          "AND table_type IN ('BASE TABLE', 'VIEW') ORDER BY 1, 2", (tuple(dbs),))
        return self._select(self.config.get("tables"), [(r[0], r[1]) for r in rows], dbs[0])

    def columns(self, c, schema: str, table: str) -> list[tuple[str, str]]:
        return [(r[0], r[1]) for r in self._q(
            c, "SELECT column_name, column_type FROM information_schema.columns WHERE table_schema = %s AND table_name = %s "
               "ORDER BY ordinal_position", (schema, table))]

    def primary_key(self, c, schema: str, table: str) -> list[str]:
        return [r[0] for r in self._q(
            c, "SELECT column_name FROM information_schema.key_column_usage WHERE table_schema = %s AND table_name = %s "
               "AND constraint_name = 'PRIMARY' ORDER BY ordinal_position", (schema, table))]

    def quote(self, name: str) -> str:
        return "`" + name.replace("`", "``") + "`"

    def stream(self, c, query: str, args, batch: int):
        with c.cursor(pymysql.cursors.SSCursor) as cur:             # unbuffered: rows arrive in batches
            cur.execute(query, args)
            while True:
                rows = cur.fetchmany(batch)
                if not rows:
                    return
                yield rows

    def arrow_type(self, db_type: str):
        import pyarrow as pa
        t = db_type.lower()
        base = t.split("(")[0].replace(" unsigned", "").strip()
        if t.startswith("tinyint(1)") or base in ("bool", "boolean"):
            return pa.bool_()
        if base in ("tinyint", "smallint"):
            return pa.int32() if "unsigned" in t else pa.int16()
        if base in ("mediumint", "int", "integer", "year"):
            return pa.int64() if "unsigned" in t else pa.int32()
        if base == "bigint":
            return pa.int64()
        if base in ("float",):
            return pa.float32()
        if base in ("double", "real", "decimal", "numeric"):
            return pa.float64()
        if base == "date":
            return pa.date32()
        if base in ("datetime", "timestamp"):
            return pa.timestamp("us")
        return pa.string()

    def duckdb_attach(self) -> str:
        d = self._dsn()
        parts = {"host": d["host"], "port": d["port"], "user": d["user"], "password": d["password"],
                 "database": d["database"]}
        return " ".join(f"{k}={v}" for k, v in parts.items() if v not in ("", None))

    def table_name(self, schema: str, table: str) -> str:
        return clean_name(table if schema == self._databases()[0] else f"{schema}_{table}")
