"""Separate workspaces per company (0.9): sign-up creates a workspace with the person as its owner; people, keys,
activity, settings, data sets and files of one workspace are invisible to another, enforced by Postgres row-level
security (FORCE, non-superuser role cl_app) and per-workspace folders. The existing install is the first workspace.
Needs a throwaway Postgres: CL_TEST_DATABASE_URL (see conftest.py)."""
import os

import pytest
from fastapi.testclient import TestClient

from app import access, core, engine
from app import db as D

pytestmark = pytest.mark.skipif(not os.environ.get("CL_TEST_DATABASE_URL"), reason="no test Postgres (CL_TEST_DATABASE_URL)")
H = {"X-CL": "1"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    core.reset_for_tests(tmp_path / "var")
    monkeypatch.setenv("CL_FILES_DIR", str(tmp_path / "workspaces"))
    monkeypatch.setenv("CL_PUBLIC_HOSTS", "testserver")
    monkeypatch.setenv("CL_SIGNUP", "open")
    monkeypatch.setattr(engine, "postgres_ok", lambda: (False, "test"))
    from app import main
    main._attempts.clear()
    with TestClient(main.asgi) as c:
        yield c


def signup(c, email, org):
    c.cookies.clear()
    r = c.post("/api/v1/signup", headers=H, json={"name": org + " owner", "email": email, "password": "pass-word-123",
                                                   "organisation": org})
    assert r.status_code == 200, r.text
    return r.json()


def test_signup_creates_separate_workspaces(client):
    c = client
    a = signup(c, "ann@acme.example", "Acme")
    ka = c.post("/api/v1/agents", headers=H, json={"name": "Acme bot", "groups": ["public"], "capabilities": ["documents"]})
    assert ka.status_code == 200, ka.text
    b = signup(c, "bob@globex.example", "Globex")
    assert a["workspace"] != b["workspace"] and a["user"]["role"] == b["user"]["role"] == "owner"
    # Globex's owner sees only Globex
    assert [u["email"] for u in c.get("/api/v1/users").json()["users"]] == ["bob@globex.example"]
    assert c.get("/api/v1/agents").json()["agents"] == []
    assert c.get("/api/v1/setup").json()["organisation"] in ("", "Acme", "Globex")   # anonymous view of the first workspace
    # the key of Acme acts in Acme
    p = core.principal_for_token(ka.json()["token"])
    assert p["tenant_id"] == a["workspace"]
    with core.tenant_scope(a["workspace"]):
        assert core.get_setting("organisation") == "Acme"
        assert [x["name"] for x in core.list_agents()] == ["Acme bot"]
        assert any(e["action"] == "signup" for e in core.list_events())
    with core.tenant_scope(b["workspace"]):
        assert core.get_setting("organisation") == "Globex"
        assert core.list_agents() == []
    from app import mcp_server as M
    text = M.describe_org()
    assert "Acme" not in text and "Globex" not in text        # shared MCP instructions name no company
    # an email belongs to one workspace
    c.cookies.clear()
    assert c.post("/api/v1/signup", headers=H, json={"name": "x", "email": "ann@acme.example", "password": "pass-word-123",
                                                     "organisation": "Other"}).status_code == 400


def test_signup_is_closed_by_default(client, monkeypatch):
    monkeypatch.setenv("CL_SIGNUP", "off")
    assert client.post("/api/v1/signup", headers=H, json={"name": "x", "email": "x@y.example", "password": "pass-word-123",
                                                          "organisation": "X"}).status_code == 403


def test_row_level_security_in_postgres(client):
    a = signup(client, "ann@acme.example", "Acme")["workspace"]
    b = signup(client, "bob@globex.example", "Globex")["workspace"]
    with core.tenant_scope(a):
        access.add_dataset("board-papers", "", "ann@acme.example")
    with core.tenant_scope(b):
        assert "board-papers" not in {d["name"] for d in access.datasets()}
        access.add_dataset("board-papers", "", "bob@globex.example")          # same name in another workspace: fine
        with D.pg_tx() as c:
            assert c.execute("SELECT count(*) FROM users").fetchone()[0] == 1
            assert c.execute("SELECT current_user").fetchone()[0] == D.APP_ROLE  # not the (superuser) database user
            # writing a row into another workspace is refused by the policy
            with pytest.raises(Exception):
                c.execute("INSERT INTO kv (key, value, tenant_id) VALUES ('x', '1', ?)", (a,))
    with core.system(), D.pg_tx() as c:
        assert c.execute("SELECT count(*) FROM users").fetchone()[0] == 2
    with D.ddl_tx() as c:
        forced = {r[0] for r in c.execute("SELECT relname FROM pg_class WHERE relforcerowsecurity")}
    assert {"users", "agents", "events", "kv", "ac_datasets", "ac_roles", "ac_team", "ac_user", "team_admins", "teams",
            "people", "invites", "upload_links", "secrets", "sources", "quality_runs", "chats"} <= forced


def test_files_live_in_each_workspace(client, tmp_path):
    a = signup(client, "ann@acme.example", "Acme")["workspace"]
    b = signup(client, "bob@globex.example", "Globex")["workspace"]
    with core.tenant_scope(b):
        engine.save_document("plan.md", b"# Globex plan", ["public"])
        assert [d["name"] for d in engine.documents()] == ["plan.md"]
        assert (tmp_path / "workspaces" / b / "documents" / "plan.md").exists()
        from semantic import config as SC
        assert (tmp_path / "workspaces" / b / "semantic_layer" / "metrics.yaml").exists()   # an empty layer
        assert str(SC.DATA_DIR).startswith(str(tmp_path / "workspaces" / b))
        from semantic import defstore
        assert defstore._schema().startswith("cl_defs_")                    # definitions in a schema of its own
    from semantic import defstore
    assert defstore._schema() == "cl_defs"
    with core.tenant_scope(a):
        assert engine.documents() == []
    # the signed-in owner of Acme sees no Globex file through the API
    client.cookies.clear()
    assert client.post("/api/v1/login", headers=H, json={"email": "ann@acme.example", "password": "pass-word-123"}).status_code == 200
    assert client.get("/api/v1/documents").json()["documents"] == []


def test_existing_install_is_the_first_workspace(client):
    r = client.post("/api/v1/setup", headers=H, json={"name": "Owner", "email": "o@example.com", "password": "pass-word-123",
                                                      "organisation": "First"})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["tenant_id"] == D.DEFAULT
    assert any(t["id"] == D.DEFAULT for t in core.list_tenants())
