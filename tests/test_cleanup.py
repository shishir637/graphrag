"""0.7 cleanup: the official postgres:17 image, the Apache AGE leftovers removed on request (python -m app.upgrade
drop-age, refused while a workspace still has a graph build file to move), and no workspace file paths left for
passages, the graph build or the enrichment store."""
from pathlib import Path

import psycopg
import pytest

from app import core, db, jobs, upgrade, vault
from tests.conftest import needs_pg

pytestmark = needs_pg
ROOT = Path(__file__).resolve().parent.parent


def test_compose_uses_the_official_postgres_image():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "image: postgres:17" in text and "shared_preload_libraries" not in text and "GRAPH_PG_DSN" not in text
    assert not (ROOT / "db" / "Dockerfile").exists() and not (ROOT / "docker-compose.graph.yml").exists()
    assert (ROOT / "experiments" / "docker-compose.graph.yml").exists()
    assert core.VERSION == "0.7.0" and "context-layer:0.7.0" in text


def test_drop_age(tmp_path):
    jobs.stop_workers()
    core.reset_for_tests(tmp_path / "var")
    vault.reset_for_tests()
    t = core.create_tenant("Acme")
    with psycopg.connect(db.url(), autocommit=True) as c:
        if not c.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'age'").fetchone():
            pytest.skip("this test database has no Apache AGE to remove")
        c.execute("CREATE EXTENSION IF NOT EXISTS age")
        c.execute("LOAD 'age'; SET search_path = ag_catalog, public")
        if not c.execute("SELECT 1 FROM ag_catalog.ag_graph WHERE name = 'kg_t_cleanup'").fetchone():
            c.execute("SELECT create_graph('kg_t_cleanup')")
    from rag.wspath import files_root
    out = files_root() / t / "outputs"
    out.mkdir(parents=True, exist_ok=True)
    (out / "_graph_build.json").write_text("{}")
    with pytest.raises(SystemExit):                       # a graph build file still waits to be moved
        upgrade.drop_age()
    (out / "_graph_build.json").unlink()
    r = upgrade.drop_age()
    assert "kg_t_cleanup" in r["schemas"] and r["extension"]
    with psycopg.connect(db.url(), autocommit=True) as c:
        assert not c.execute("SELECT 1 FROM pg_extension WHERE extname = 'age'").fetchone()
        assert not c.execute("SELECT 1 FROM pg_namespace WHERE nspname IN ('ag_catalog', 'kg_t_cleanup')").fetchone()
    assert upgrade.drop_age() == {"schemas": [], "extension": False}           # idempotent


def test_no_workspace_file_paths_for_moved_data():
    import re
    for folder in ("app", "rag", "graph", "semantic", "facts", "connectors"):
        for p in (ROOT / folder).glob("*.py"):
            src = p.read_text(encoding="utf-8")
            assert "enrichment.duckdb" not in src, p
            if p.name not in ("passages.py", "kg.py", "engine.py", "nlp_extract.py", "upgrade.py", "evaluate.py",
                              "extract_llm.py", "extract_trad.py", "wspath.py", "chunk.py", "build.py", "search.py"):
                assert not re.search(r"2_chunks\.json|_graph_build\.json", src), p
