"""Data for metrics: a cached DuckDB per workspace, row access rules applied inside every query, and the Postgres
connector (snapshot, incremental, live) feeding drafted definitions. Uses the test Postgres as the source."""
import os

import psycopg
import pytest

from app import core, jobs, vault
from connectors import manage
from semantic import access, repo
from tests.conftest import needs_pg, seed

pytestmark = needs_pg
DSN = os.environ["CL_DATABASE_URL"]


@pytest.fixture
def ws(tmp_path):
    jobs.stop_workers()
    core.reset_for_tests(tmp_path / "var")
    vault.reset_for_tests()
    a = core.create_tenant("Acme")
    seed(a)
    return a


def test_row_access_rules_apply_inside_queries(ws):
    from app import engine
    with core.tenant_scope(ws):
        req = {"metrics": ["order_count"], "dimensions": ["orders.channel"]}
        everyone = engine.metric_query(req, groups=None)
        channels = {r["orders.channel"] if "orders.channel" in r else list(r.values())[0] for r in everyone["rows"]}
        assert len(channels) >= 2
        access.put_rule("orders", "channel", "app-team", ["app"])
        mine = engine.metric_query(req, groups=["app-team"])
        assert [list(r.values())[0] for r in mine["rows"]] == ["app"]                 # only the allowed rows
        none = engine.metric_query(req, groups=["public"])
        assert none["found"] is False                                                 # rules exist, none for them
        assert engine.metric_query(req, groups=None)["row_count"] == everyone["row_count"]   # owners: everything
        d = engine.describe_data(["app-team"])
        ch = next(x for x in d["dimensions"] if x["name"] == "orders.channel")
        assert ch.get("values") == ["app"]
        assert "app-team" in engine.all_groups()


def test_duckdb_is_cached_per_workspace_and_follows_file_changes(ws):
    from semantic import db
    from semantic.config import DATA_DIR
    with core.tenant_scope(ws):
        b1 = db.base()
        assert db.base() is b1                                                        # cached
        (DATA_DIR / "extra.csv").write_text("x\n1\n")
        b2 = db.base()
        assert b2 is not b1 and db.connect().execute('SELECT count(*) FROM "extra"').fetchone()[0] == 1


@pytest.fixture
def source():
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS src_shop CASCADE; CREATE SCHEMA src_shop;"
                  "CREATE TABLE src_shop.stores (store_id text PRIMARY KEY, city text);"
                  "CREATE TABLE src_shop.sales (sale_id int PRIMARY KEY, store_id text REFERENCES src_shop.stores, "
                  "amount numeric, sold_at date, updated_at timestamptz DEFAULT now());"
                  "INSERT INTO src_shop.stores VALUES ('S1','Pune'),('S2','Goa');"
                  "INSERT INTO src_shop.sales VALUES (1,'S1',100,'2026-10-01', '2026-10-01'),(2,'S2',50,'2026-10-02','2026-10-02'),"
                  "(3,'S1',25,'2026-10-03','2026-10-03');")
    yield
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS src_shop CASCADE")


def test_postgres_snapshot_drafts_metrics_and_is_incremental(ws, source):
    from app import engine
    from semantic.config import DATA_DIR
    with core.tenant_scope(ws):
        c = manage.create("postgres", "Shop DB", {"schemas": ["src_shop"], "updated_column": "updated_at",
                                                  "batch_rows": 2}, {"dsn": DSN}, None, 60, "ann")
        assert manage.test(c["id"])["ok"]
        manage.run_sync({"connector": c["id"]})                     # copies, then runs the data build
        assert _rows("src_shop_sales") == 3 and not (DATA_DIR / "src_shop_sales.csv").exists()   # Parquet, 2 batches
        drafts = {r["name"]: r for r in repo.rows("metric") if r["status"] == "draft"}
        assert any("src_shop_sales" in n or "sales" in n for n in drafts), list(drafts)
        ents = {r["name"] for r in repo.rows("entity")}
        assert {"src_shop_sales", "src_shop_stores"} <= ents
        # a change at the source: only that row is read and merged
        with psycopg.connect(DSN, autocommit=True) as pc:
            pc.execute("UPDATE src_shop.sales SET amount = 999, updated_at = '2026-11-01' WHERE sale_id = 2")
        manage.run_sync({"connector": c["id"]})
        assert _rows("src_shop_sales") == 3 and _rows("src_shop_sales", "amount = 999") == 1
        from app import landing
        h = landing.history("src_shop_sales.parquet")
        assert len(h) == 2 and h[-1]["source"] == "connector" and h[-1]["connector_id"] == c["id"]
        manage.run_sync({"connector": c["id"]})                     # nothing changed at the source: no new landing
        assert len(landing.history("src_shop_sales.parquet")) == 2
        q = engine.metric_query({"metrics": [next(n for n in drafts if "total" in n and "amount" in n)]}, groups=None)
        assert float(list(q["rows"][0].values())[0]) == 100 + 999 + 25


def _rows(table, where="true"):
    import duckdb
    from app.storage import store
    path = store().path_for_duckdb("data", f"{table}.parquet")
    return duckdb.connect().execute(f"SELECT count(*) FROM read_parquet('{path}') WHERE {where}").fetchone()[0]


def test_postgres_live_mode_queries_the_source(ws, source):
    from semantic import db
    with core.tenant_scope(ws):
        c = manage.create("postgres", "Shop live", {"schemas": ["src_shop"], "mode": "live"}, {"dsn": DSN}, None, 60, "ann")
        manage.run_sync({"connector": c["id"]})
        assert db.connect().execute('SELECT sum(amount) FROM "src_shop_sales"').fetchone()[0] == 175
        with psycopg.connect(DSN, autocommit=True) as pc:
            pc.execute("INSERT INTO src_shop.sales VALUES (4,'S2',5,'2026-11-02')")
        assert db.connect().execute('SELECT sum(amount) FROM "src_shop_sales"').fetchone()[0] == 180   # always current
