"""The first sign-in guide: role-aware steps ticked from real state, dismissal per person and workspace."""
import pytest
from fastapi.testclient import TestClient

from app import core, engine, vault
from tests.conftest import needs_pg

pytestmark = needs_pg
H = {"X-CL": "1"}


@pytest.fixture
def c(tmp_path, monkeypatch):
    core.reset_for_tests(tmp_path / "var")
    vault.reset_for_tests()
    monkeypatch.setenv("CL_PUBLIC_HOSTS", "testserver")
    monkeypatch.setenv("CL_SIGNUP", "open")
    monkeypatch.setenv("CL_SEED_LAYER", "0")
    monkeypatch.setattr(engine, "postgres_ok", lambda: (False, "test"))
    from app import main
    main._attempts.clear()
    with TestClient(main.asgi) as client:
        yield client


def ids(g):
    return {s["id"]: s["done"] for s in g["steps"]}


def test_owner_steps_follow_real_state(c):
    assert c.post("/api/v1/signup", headers=H, json={"name": "Ann", "email": "ann@acme.io", "password": "pass-word-123",
                                                     "organisation": ""}).status_code == 200
    g = c.get("/api/v1/guide").json()
    assert g["show"] and list(ids(g)) == ["name", "source", "build", "review", "connect", "ask"]
    assert not any(ids(g).values())
    connect = next(s for s in g["steps"] if s["id"] == "connect")
    assert "claude mcp add --transport http" in connect["snippets"]["claude_code"] and connect["mcp_url"].endswith("/mcp")
    c.put("/api/v1/settings/organisation", headers=H, json={"organisation": "Acme"})
    c.post("/api/v1/agents", headers=H, json={"name": "Bot", "groups": ["public"]})
    assert c.post("/api/v1/guide/sample", headers=H).status_code == 200                     # sample data
    assert c.post("/api/v1/guide/sample", headers=H).status_code == 409                     # only once
    c.post("/api/v1/playground", headers=H, json={"question": "How many orders were placed?", "mode": "metrics"})
    d = ids(c.get("/api/v1/guide").json())
    assert d["name"] and d["source"] and d["build"] and d["connect"] and d["ask"]
    ov = {x["id"]: x["done"] for x in c.get("/api/v1/overview").json()["checklist"]}
    assert ov["named"] and ov["source"] and ov["ask"] and ov["agent"]


def test_viewer_gets_the_short_guide_and_dismissal_is_per_workspace(c):
    c.post("/api/v1/signup", headers=H, json={"name": "Ann", "email": "ann@acme.io", "password": "pass-word-123",
                                              "organisation": "Acme"})
    c.post("/api/v1/users", headers=H, json={"email": "vic@acme.io", "name": "Vic", "role": "viewer",
                                             "password": "viewer-pass-1"})
    c.cookies.clear()
    c.post("/api/v1/signup", headers=H, json={"name": "Bea", "email": "bea@globex.io", "password": "pass-word-123",
                                              "organisation": "Globex"})
    c.post("/api/v1/users", headers=H, json={"email": "vic@acme.io", "name": "Vic", "role": "viewer", "password": "x" * 10})
    c.cookies.clear()
    me = c.post("/api/v1/login", headers=H, json={"email": "vic@acme.io", "password": "viewer-pass-1"}).json()["user"]
    g = c.get("/api/v1/guide").json()
    assert g["role"] == "viewer" and list(ids(g)) == ["see", "connect", "ask"]
    assert "public" in g["steps"][0]["body"]
    c.post("/api/v1/guide/dismiss", headers=H)
    assert c.get("/api/v1/guide").json()["show"] is False                                  # remembered
    other = next(w["id"] for w in c.get("/api/v1/me").json()["workspaces"] if w["id"] != me["tenant_id"])
    c.post("/api/v1/workspaces/switch", headers=H, json={"workspace": other})
    assert c.get("/api/v1/guide").json()["show"] is True                                   # other workspace: not dismissed
    c.post("/api/v1/workspaces/switch", headers=H, json={"workspace": me["tenant_id"]})
    c.post("/api/v1/guide/reset", headers=H)
    assert c.get("/api/v1/guide").json()["show"] is True
