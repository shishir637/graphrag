"""Connector families (0.7): a SQL base (Postgres and MySQL on it: batched Parquet snapshots, incremental merge by
primary key, live mode through DuckDB) and a file-source base (GitHub and an S3-compatible folder on it: change
detection, removals, groups). MySQL runs against a throwaway server (CL_TEST_MYSQL_URL, default
mysql://root:testpw@127.0.0.1:3399/shop); S3 uses a stand-in client."""
import os
from contextlib import nullcontext

import pytest

from app import core, jobs, landing, vault
from connectors import manage
from tests.conftest import needs_pg

pytestmark = needs_pg
MYSQL = os.environ.get("CL_TEST_MYSQL_URL", "mysql://root:testpw@127.0.0.1:3399/shop")


@pytest.fixture
def ws(tmp_path):
    jobs.stop_workers()
    core.reset_for_tests(tmp_path / "var")
    vault.reset_for_tests()
    return core.create_tenant("Acme")


def test_families():
    from connectors.files_base import FileSource
    from connectors.sql_base import SqlSource
    assert {k for k, c in manage.KINDS.items() if issubclass(c, SqlSource)} == {"postgres", "mysql"}
    assert {k for k, c in manage.KINDS.items() if issubclass(c, FileSource)} == {"github", "s3"}


# ------------------------------------------------------------------ MySQL on the SQL base

def _mysql():
    try:
        import pymysql
        from connectors.mysql import parse_dsn
        d = parse_dsn(MYSQL)
        c = pymysql.connect(host=d["host"], port=d["port"], user=d["user"], password=d["password"], autocommit=True,
                            connect_timeout=3)
        return c, d["database"]
    except Exception:                                       # noqa: BLE001
        return None, None


@pytest.fixture
def shop(monkeypatch):
    from app import engine
    monkeypatch.setattr(engine, "rebuild", lambda info: None)    # the data build itself is tested elsewhere
    c, db = _mysql()
    if c is None:
        pytest.skip("no test MySQL")
    with c.cursor() as cur:
        for s in (f"CREATE DATABASE IF NOT EXISTS `{db}`", f"USE `{db}`", "DROP TABLE IF EXISTS sales", "DROP TABLE IF EXISTS stores",
                  "CREATE TABLE stores (store_id VARCHAR(8) PRIMARY KEY, city VARCHAR(40))",
                  "CREATE TABLE sales (sale_id INT PRIMARY KEY, store_id VARCHAR(8), amount DECIMAL(10,2), sold_on DATE, "
                  "open_flag TINYINT(1), updated_at DATETIME)",
                  "INSERT INTO stores VALUES ('S1','Pune'),('S2','Goa')",
                  "INSERT INTO sales VALUES (1,'S1',100,'2026-10-01',1,'2026-10-01 10:00:00'),"
                  "(2,'S2',50,'2026-10-02',0,'2026-10-02 10:00:00'),(3,'S1',25,'2026-10-03',1,'2026-10-03 10:00:00')"):
            cur.execute(s)
    yield c
    c.close()


def _rows(table, where="true"):
    import duckdb
    from app.storage import store
    return duckdb.connect().execute(f"SELECT count(*) FROM read_parquet('{store().path_for_duckdb('data', table + '.parquet')}') "
                                    f"WHERE {where}").fetchone()[0]


def test_mysql_snapshot_is_batched_typed_and_incremental(ws, shop):
    from semantic.db import connect
    with core.tenant_scope(ws):
        c = manage.create("mysql", "Shop", {"updated_column": "updated_at", "batch_rows": 2}, {"dsn": MYSQL}, None, 60, "ann")
        assert manage.test(c["id"])["ok"]
        manage.run_sync({"connector": c["id"]})
        assert _rows("sales") == 3 and _rows("stores") == 2
        types = dict(connect().execute('DESCRIBE "sales"').fetchall()[i][:2] for i in range(6))
        assert types["amount"] == "DOUBLE" and types["sold_on"] == "DATE" and types["open_flag"] == "BOOLEAN"
        with shop.cursor() as cur:
            cur.execute("UPDATE sales SET amount = 999, updated_at = '2026-11-01 10:00:00' WHERE sale_id = 2")
        manage.run_sync({"connector": c["id"]})
        assert _rows("sales") == 3 and _rows("sales", "amount = 999") == 1                # merged by primary key
        h = landing.history("sales.parquet")
        assert len(h) == 2 and h[-1]["connector_id"] == c["id"]
        assert connect().execute('SELECT sum(amount) FROM "sales"').fetchone()[0] == 100 + 999 + 25


def test_mysql_live_mode(ws, shop):
    from semantic import db
    with core.tenant_scope(ws):
        c = manage.create("mysql", "Shop live", {"mode": "live"}, {"dsn": MYSQL}, None, 60, "ann")
        manage.run_sync({"connector": c["id"]})
        src = manage.live_sources()
        assert src and src[0]["type"] == "mysql" and "password=" in src[0]["attach"]
        try:
            assert db.connect().execute('SELECT sum(amount) FROM "sales"').fetchone()[0] == 175
        except Exception as e:                                  # noqa: BLE001
            if "extension" in str(e).lower():                  # e.g. blocked by Windows Application Control
                pytest.skip(f"DuckDB mysql extension not available here: {e}")
            raise
        with shop.cursor() as cur:
            cur.execute("INSERT INTO sales VALUES (4,'S2',5,'2026-11-02',1,'2026-11-02 10:00:00')")
        assert db.connect().execute('SELECT sum(amount) FROM "sales"').fetchone()[0] == 180          # always current


# ------------------------------------------------------------------ an S3-compatible folder on the file base

class FakeS3:
    def __init__(self, objects):
        self.objects = objects                              # key -> (etag, bytes)
        self.gets = []

    def list_objects_v2(self, Bucket, Prefix="", ContinuationToken=None, MaxKeys=1000):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start:start + 2]                         # small pages: pagination is exercised
        more = start + 2 < len(keys)
        return {"Contents": [{"Key": k, "ETag": f'"{self.objects[k][0]}"', "Size": len(self.objects[k][1])} for k in page],
                "IsTruncated": more, "NextContinuationToken": str(start + 2) if more else None}

    def get_object(self, Bucket, Key):
        import io
        self.gets.append(Key)
        return {"Body": io.BytesIO(self.objects[Key][1])}


def test_s3_folder_syncs_changes_and_removals(ws, monkeypatch):
    from connectors.s3 import S3Folder
    from rag import registry
    fake = FakeS3({"contracts/msa.md": ("e1", b"# MSA\nNotice 30 days."), "contracts/nda.txt": ("e2", b"NDA text"),
                   "contracts/logo.png": ("e3", b"\x89PNG"), "contracts/sub/sow.md": ("e4", b"# SOW"),
                   "other/skip.md": ("e5", b"not in the prefix")})
    monkeypatch.setattr(S3Folder, "client", lambda self: nullcontext(fake))
    rebuilt = []
    from app import engine
    monkeypatch.setattr(engine, "rebuild", lambda info: rebuilt.append(info))
    with core.tenant_scope(ws):
        c = manage.create("s3", "Contracts", {"bucket": "acme-docs", "prefix": "contracts/"},
                          {"access_key_id": "AKIA_TEST", "secret_access_key": "x"}, ["legal"], 60, "ann")
        manage.run_sync({"connector": c["id"]})
        docs = registry.access_map()
        assert set(docs) == {"s3-acme-docs--contracts__msa.md", "s3-acme-docs--contracts__nda.txt",
                             "s3-acme-docs--contracts__sub__sow.md"}                  # png skipped, other prefix skipped
        assert all(g == ["legal"] for g in docs.values())
        assert {r["source"] for r in landing.current("document")} == {"connector"}
        fake.gets.clear()
        manage.run_sync({"connector": c["id"]})
        assert fake.gets == []                                                       # nothing changed: nothing fetched
        fake.objects["contracts/msa.md"] = ("e9", b"# MSA\nNotice 60 days.")
        del fake.objects["contracts/nda.txt"]
        manage.run_sync({"connector": c["id"]})
        assert fake.gets == ["contracts/msa.md"]
        assert "s3-acme-docs--contracts__nda.txt" not in registry.access_map()
        assert len(landing.history("s3-acme-docs--contracts__msa.md", "document")) == 2
        assert manage.get(c["id"])["secrets"]["secret_access_key"] != "x"              # secrets never shown again
