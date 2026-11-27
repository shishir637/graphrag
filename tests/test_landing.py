"""Tables as Parquet read by DuckDB views (0.9): uploads and source syncs land as Parquet (zstd) with a manifest
entry (source, who, rows, columns, hash), DuckDB reads them through views (no copy), older CSV/Excel tables are
converted by the backfill, and row rules still apply on views. No models."""
import json

import duckdb
import pytest

from app import engine, landing
from semantic import config as SC
from semantic import db as SDB


@pytest.fixture
def data(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(SC, "DATA_DIR", d)
    monkeypatch.setattr(SDB, "DATA_DIR", d)
    from semantic import ingest
    monkeypatch.setattr(ingest, "DATA_DIR", d)
    monkeypatch.setattr(ingest, "UPLOAD_DIR", d / "_uploads")
    return d


def test_upload_lands_as_parquet_with_a_manifest(data):
    assert engine.save_table("Sales Q1.csv", b"region,amount\nNorth,10\nSouth,20\n") == ["sales_q1"]
    assert (data / "sales_q1.parquet").exists() and not (data / "sales_q1.csv").exists()
    m = landing.manifest()["sales_q1"]
    assert m["rows"] == 2 and m["columns"] == ["region", "amount"] and m["source"] == "upload"
    assert m["original"] == "Sales Q1.csv" and len(m["sha256"]) == 64
    con = SDB.connect()
    kinds = dict(con.execute("SELECT table_name, table_type FROM information_schema.tables").fetchall())
    assert kinds["sales_q1"] == "VIEW"                                   # read at query time, never copied
    assert con.execute("SELECT sum(amount) FROM sales_q1").fetchone()[0] == 30


def test_source_tables_land_with_their_source(data):
    from sources.files import store_table
    assert store_table("github_repos", csv=b"repo_id,stars\nacme/api,3\n", source="source:s_1")
    assert landing.manifest()["github_repos"]["source"] == "source:s_1"
    assert not store_table("github_repos", csv=b"repo_id,stars\nacme/api,3\n", source="source:s_1")   # unchanged


def test_backfill_converts_older_tables(data):
    (data / "orders.csv").write_text("order_id,amount\nO1,5\nO2,7\n", encoding="utf-8")
    out = landing.backfill()
    assert out["converted"] == ["orders.csv"]
    assert (data / "orders.parquet").exists() and not (data / "orders.csv").exists()
    assert (data / "_uploads" / "orders.csv").exists()                   # the original is kept
    assert landing.manifest()["orders"]["source"] == "backfill"
    assert landing.backfill()["converted"] == []                         # idempotent
    p = str(data / "orders.parquet").replace("\\", "/")
    assert duckdb.connect().execute(f"SELECT sum(amount) FROM read_parquet('{p}')").fetchone()[0] == 12


def test_unfinished_landings_are_not_tables(data):
    (data / ".x.landing.parquet").write_bytes(b"")
    (data / "_manifest.json").write_text(json.dumps({}))
    assert SDB.table_files() == {}
