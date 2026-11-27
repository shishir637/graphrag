"""DuckDB views over the landing zone (0.7): the workspace's DuckDB holds only views over the Parquet files, row
access rules still shadow them, profiling and every metric give the same results as the old in-memory copies,
and queries get a memory limit and a spill folder in the workspace's volume."""
from pathlib import Path

import duckdb
import pytest

from app import core, jobs, vault
from tests.conftest import needs_pg, seed

pytestmark = needs_pg
LUMORA = Path(__file__).resolve().parent.parent / "test-data" / "lumora" / "tables"


@pytest.fixture
def ws(tmp_path):
    jobs.stop_workers()
    core.reset_for_tests(tmp_path / "var")
    vault.reset_for_tests()
    t = core.create_tenant("Acme")
    seed(t)
    from app import engine
    with core.tenant_scope(t):
        for p in sorted(LUMORA.iterdir()):
            engine.save_table(p.name, p.read_bytes())
    return t


def _copies(files: dict) -> duckdb.DuckDBPyConnection:
    """How 0.6 built the workspace database: every table copied into memory."""
    from semantic.db import _reader
    con = duckdb.connect()
    for name, path in files.items():
        con.execute(f'CREATE TABLE "{name}" AS SELECT * FROM {_reader(path)}')
    return con


def test_catalog_is_views_with_memory_limit_and_spill_folder(ws):
    from semantic import db
    with core.tenant_scope(ws):
        con = db.connect()
        kinds = dict(con.execute("SELECT table_name, table_type FROM information_schema.tables "
                                 "WHERE table_schema = 'main'").fetchall())
        assert kinds and set(kinds.values()) == {"VIEW"}
        assert {"orders", "lumora_invoices", "lumora_customers"} <= set(kinds)
        assert all(p.endswith(".parquet") for p in db.table_files().values())
        tmp = con.execute("SELECT current_setting('temp_directory')").fetchone()[0]
        assert ws in tmp and tmp.endswith("duckdb_tmp")
        assert con.execute("SELECT current_setting('memory_limit')").fetchone()[0]


def test_row_rules_shadow_views(ws):
    from semantic import access, db
    with core.tenant_scope(ws):
        total = db.connect().execute('SELECT count(*) FROM "orders"').fetchone()[0]
        access.put_rule("orders", "channel", "app-team", ["app"])
        cur = db.connect_for(["app-team"])
        assert {r[0] for r in cur.execute('SELECT DISTINCT channel FROM "orders"').fetchall()} == {"app"}
        assert cur.execute('SELECT count(*) FROM "orders"').fetchone()[0] < total
        assert db.connect_for(["public"]).execute('SELECT count(*) FROM "orders"').fetchone()[0] == 0
        assert db.connect().execute('SELECT count(*) FROM "orders"').fetchone()[0] == total   # base unchanged


def test_profiling_and_metrics_match_the_old_copies(ws, monkeypatch):
    from semantic import db, profile
    from semantic.compiler import compile_query
    from semantic.schema import SemanticQuery
    from semantic.yamlio import load_metrics, load_model
    with core.tenant_scope(ws):
        views = profile.profile_all()
        old = _copies(db.table_files())
        with monkeypatch.context() as mp:           # not undo(): that would also undo the workspace-folder fixture
            mp.setattr(db, "base", lambda: old)
            copies = profile.profile_all()
        assert views == copies
        model, metrics = load_model(), load_metrics()
        checked = 0
        for m in metrics.metrics:
            if m.status != "approved":
                continue
            from semantic.compiler import CompileError
            try:
                c = compile_query(SemanticQuery(metrics=[m.name]), model, metrics)
            except CompileError:                       # running totals need a time dimension
                c = compile_query(SemanticQuery(metrics=[m.name], dimensions=[f"{m.time_column}__month"]), model, metrics)
            a = db.connect().execute(c.sql).fetchall()
            b = old.cursor().execute(c.sql).fetchall()
            assert a == b, m.name
            checked += 1
        assert checked >= 10
