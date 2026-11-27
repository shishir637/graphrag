"""The landing zone (0.7): every table lands as Parquet through the storage interface, every landing writes a
manifest row (who pushed it, sha256, rows, schema), identical files change nothing, and workspaces from before
0.7 are upgraded automatically (CSV -> Parquet with the same column types, manifest rows) and idempotently."""
import json

import duckdb
import pytest

from app import core, jobs, landing, vault
from app.storage import LocalStore, store
from tests.conftest import needs_pg

pytestmark = needs_pg
CSV = b"order_id,city,amount,ordered_on\n1,Pune,10.5,2026-10-01\n2,Goa,20,2026-10-02\n"


@pytest.fixture
def ws(tmp_path):
    jobs.stop_workers()
    core.reset_for_tests(tmp_path / "var")
    vault.reset_for_tests()
    return core.create_tenant("Acme")


def test_local_store_basics(ws):
    st = store()
    assert isinstance(st, LocalStore)
    with core.tenant_scope(ws):
        p = st.put("documents", "a.md", b"hello")
        assert ws in p and st.exists("documents", "a.md") and st.get("documents", "a.md") == b"hello"
        assert st.list("documents") == ["a.md"] and st.stat("documents", "a.md")["bytes"] == 5
        with st.open("documents", "a.md") as f:
            assert f.read() == b"hello"
        with pytest.raises(ValueError):
            st.put("documents", "../escape.md", b"x")
        assert st.delete("documents", "a.md") and not st.exists("documents", "a.md")
    assert not st.exists("documents", "a.md")                    # outside the workspace: another folder


def test_csv_upload_lands_as_parquet_with_manifest(ws):
    from app import engine
    with core.tenant_scope(ws):
        assert engine.save_table("Orders 2026.csv", CSV, by="ann@acme.test", source="web") == ["orders_2026"]
        st = store()
        assert st.exists("data", "orders_2026.parquet") and not st.exists("data", "orders_2026.csv")
        rows = landing.current("table")
        assert len(rows) == 1
        r = rows[0]
        assert (r["name"], r["rows"], r["pushed_by"], r["source"]) == ("orders_2026.parquet", 2, "ann@acme.test", "web")
        types = {c["name"]: c["type"] for c in r["schema"]}
        assert types["amount"] == "double" and types["ordered_on"] == "date32[day]"   # DuckDB's own inference
        # the same file again: nothing changes
        engine.save_table("Orders 2026.csv", CSV, by="ann@acme.test")
        assert len(landing.history("orders_2026.parquet")) == 1
        engine.save_table("Orders 2026.csv", CSV + b"3,Pune,5,2026-10-03\n", by="bob@acme.test")
        h = landing.history("orders_2026.parquet")
        assert len(h) == 2 and h[0]["replaced_at"] and h[1]["rows"] == 3 and h[1]["replaced_at"] is None
        from semantic.db import connect
        assert connect().execute('SELECT sum(amount) FROM "orders_2026"').fetchone()[0] == 35.5


def test_documents_and_agent_uploads_write_the_manifest(ws):
    from app import engine, mcp_server
    with core.tenant_scope(ws):
        engine.save_document("policy.md", b"# Leave\n24 days", ["hr"], by="ann@acme.test", source="web")
        engine.save_document("policy.md", b"# Leave\n25 days", ["hr"], by="ann@acme.test", source="web")
        h = landing.history("policy.md", "document")
        assert len(h) == 2 and h[1]["groups"] == ["hr"] and h[0]["replaced_at"]
        agent = {"name": "relay-bot", "capabilities": ["documents", "upload"], "groups": ["public"], "scope": "agent"}
        out = mcp_server.tool_add_document(agent, filename="note.md", text="Agents can push notes.")
        assert out["saved"]["kind"] == "document"
        r = next(x for x in landing.current("document") if x["name"] == "note.md")
        assert (r["source"], r["pushed_by"]) == ("mcp", "relay-bot")
        out = mcp_server.tool_add_document(agent, filename="kpis.csv", text="k,v\na,1\n", kind="table")
        assert out["saved"]["tables"] == ["kpis"]
        assert next(x for x in landing.current("table") if x["name"] == "kpis.parquet")["pushed_by"] == "relay-bot"
        with pytest.raises(Exception):                            # the upload capability check stays
            mcp_server.tool_add_document(agent | {"capabilities": ["documents"]}, filename="x.md", text="x")
        engine.delete_document("policy.md")
        assert not [x for x in landing.current("document") if x["name"] == "policy.md"]
        assert len(landing.history("policy.md", "document")) == 2          # history kept


def test_backfill_converts_old_workspaces_and_is_idempotent(ws):
    from semantic.config import DATA_DIR
    from rag.config import DOCS_DIR
    from rag import registry
    with core.tenant_scope(ws):
        d = DATA_DIR.resolve_now()
        d.mkdir(parents=True)
        (d / "sales.csv").write_bytes(CSV)                         # how 0.6 kept tables
        before = duckdb.connect().execute(
            f"DESCRIBE SELECT * FROM read_csv_auto('{str(d / 'sales.csv').replace(chr(92), '/')}', header=true)").fetchall()
        DOCS_DIR.resolve_now().mkdir(parents=True)
        (DOCS_DIR.resolve_now() / "old.md").write_text("an old document")
        registry.set_groups("old.md", ["hr"])
        out = landing.backfill()
        assert out == {"tables_converted": 1, "manifest_rows": 2}
        assert (d / "sales.parquet").exists() and not (d / "sales.csv").exists()
        assert (d / "_uploads" / "sales.csv").read_bytes() == CSV                 # nothing lost
        after = duckdb.connect().execute(
            f"DESCRIBE SELECT * FROM read_parquet('{str(d / 'sales.parquet').replace(chr(92), '/')}')").fetchall()
        assert [(r[0], r[1]) for r in before] == [(r[0], r[1]) for r in after]  # same column types
        doc = next(x for x in landing.current("document"))
        assert doc["groups"] == ["hr"] and doc["source"] == "backfill"
        assert landing.backfill() == {"tables_converted": 0, "manifest_rows": 0}  # second run: nothing
        assert len(landing.current()) == 2


def test_upgrade_runs_every_workspace_once(ws):
    from app import upgrade
    from semantic.config import DATA_DIR
    b = core.create_tenant("Globex")
    for t in (ws, b):
        with core.tenant_scope(t):
            DATA_DIR.resolve_now().mkdir(parents=True, exist_ok=True)
            (DATA_DIR.resolve_now() / "t.csv").write_bytes(CSV)
    done = upgrade.run()
    assert set(done) == {ws, b} and done[ws]["landing"]["tables_converted"] == 1
    assert upgrade.run() == {}
    with core.tenant_scope(b):
        assert [r["name"] for r in landing.current()] == ["t.parquet"]
        ev = core.list_events("upgrade.0.7") if "type" in core.list_events.__code__.co_varnames else core.list_events()
        assert any(e["action"] == "upgrade.0.7" for e in ev)
    with core.tenant_scope(ws):
        assert json.loads(json.dumps(landing.current()))[0]["tenant_id"] == ws
