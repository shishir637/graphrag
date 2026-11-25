"""Workspaces (tenants): sign-up, isolation of keys / activity / settings, row-level security in Postgres,
people in several workspaces, and tool lists filtered by what a key may do. No models needed."""
import pytest
from fastapi.testclient import TestClient

from app import core, engine
from tests.conftest import needs_pg

pytestmark = needs_pg
H = {"X-CL": "1"}
HDR = {"accept": "application/json, text/event-stream"}


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    core.reset_for_tests(tmp_path / "var")
    docs, out = tmp_path / "documents", tmp_path / "outputs"
    docs.mkdir(); out.mkdir()
    monkeypatch.setattr(engine, "DOCS_DIR", docs)
    monkeypatch.setattr(engine, "ACCESS_FILE", docs / "_access.json")
    monkeypatch.setattr(engine, "OUT_DIR", out)
    monkeypatch.setattr(engine, "postgres_ok", lambda: (False, "test"))
    monkeypatch.setenv("CL_PUBLIC_HOSTS", "testserver")
    monkeypatch.setenv("CL_SIGNUP", "open")
    from app import main
    main._attempts.clear()
    with TestClient(main.asgi) as c:
        yield c


def signup(c, email, org):
    c.cookies.clear()
    r = c.post("/api/v1/signup", headers=H, json={"name": org, "email": email, "password": "pass-word-123",
                                                   "organisation": org})
    assert r.status_code == 200, r.text
    return r.json()["user"]


def tools(c, token):
    r = c.post("/mcp", headers=HDR | {"authorization": f"Bearer {token}"},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    return {t["name"] for t in r.json()["result"]["tools"]}


def test_signup_closed_by_default(app_client, monkeypatch):
    monkeypatch.setenv("CL_SIGNUP", "invite")
    r = app_client.post("/api/v1/signup", headers=H, json={"name": "X", "email": "x@x.io", "password": "pass-word-123"})
    assert r.status_code == 403


def test_two_workspaces_are_isolated(app_client):
    c = app_client
    a = signup(c, "a@acme.io", "Acme")
    ka = c.post("/api/v1/agents", headers=H, json={"name": "Acme bot", "groups": ["public"]}).json()
    assert c.get("/api/v1/me").json()["workspace"]["name"] == "Acme"
    b = signup(c, "b@globex.io", "Globex")
    assert a["tenant_id"] != b["tenant_id"]
    kb = c.post("/api/v1/agents", headers=H, json={"name": "Globex bot", "groups": ["public"]}).json()
    # Globex sees only its own key, activity and settings
    assert [x["name"] for x in c.get("/api/v1/agents").json()["agents"]] == ["Globex bot"]
    assert all("Acme" not in e["target"] for e in c.get("/api/v1/events").json()["events"])
    assert c.get("/api/v1/me").json()["organisation"] == "Globex"
    assert c.post(f"/api/v1/agents/{ka['agent']['id']}/revoke", headers=H).status_code == 404
    # each key works in its own workspace only
    for k, name in ((ka, "Acme bot"), (kb, "Globex bot")):
        hit = c.post("/mcp", headers=HDR | {"authorization": f"Bearer {k['token']}"},
                     json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": "list_documents", "arguments": {}}})
        assert hit.status_code == 200
    with core.tenant_scope(a["tenant_id"]):
        assert {e["actor"] for e in core.list_events(actor_type="agent")} == {"Acme bot"}
    with core.tenant_scope(b["tenant_id"]):
        assert {e["actor"] for e in core.list_events(actor_type="agent")} == {"Globex bot"}


def test_row_level_security_holds_without_app_filters(app_client):
    c = app_client
    a = signup(c, "a@acme.io", "Acme")
    c.post("/api/v1/agents", headers=H, json={"name": "Acme bot", "groups": ["public"]})
    b = signup(c, "b@globex.io", "Globex")
    c.post("/api/v1/agents", headers=H, json={"name": "Globex bot", "groups": ["public"]})
    with core.tenant_scope(a["tenant_id"]):
        # a query with no WHERE clause still only sees this workspace
        assert [r["name"] for r in core.db().execute("SELECT name FROM agents")] == ["Acme bot"]
        assert core.db().execute("SELECT count(*) FROM settings WHERE tenant_id = ?", (b["tenant_id"],)).fetchone()[0] == 0
        with pytest.raises(Exception):                   # cannot write into another workspace
            core.db().execute("INSERT INTO settings (tenant_id, key, value) VALUES (?, 'x', '1')", (b["tenant_id"],))
    with core.tenant_scope(None):                        # no workspace in scope: nothing visible
        assert core.db().execute("SELECT count(*) FROM agents").fetchone()[0] == 0


def test_person_in_two_workspaces(app_client):
    c = app_client
    signup(c, "a@acme.io", "Acme")
    b = signup(c, "b@globex.io", "Globex")
    # Globex's owner adds the Acme owner as a viewer: existing account, own password kept
    r = c.post("/api/v1/users", headers=H, json={"email": "a@acme.io", "name": "A", "role": "viewer", "password": "ignored-x1"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "viewer"
    c.cookies.clear()
    me = c.post("/api/v1/login", headers=H, json={"email": "a@acme.io", "password": "pass-word-123"}).json()["user"]
    assert me["role"] == "owner"                         # first workspace: Acme
    ws = c.get("/api/v1/me").json()["workspaces"]
    assert [w["name"] for w in ws] == ["Acme", "Globex"]
    sw = c.post("/api/v1/workspaces/switch", headers=H, json={"workspace": b["tenant_id"]}).json()["user"]
    assert sw["role"] == "viewer" and sw["tenant_id"] == b["tenant_id"]
    assert c.post("/api/v1/agents", headers=H, json={"name": "nope"}).status_code == 403
    assert c.post("/api/v1/workspaces/switch", headers=H, json={"workspace": "t_nope"}).status_code == 403


def test_tool_list_follows_the_key(app_client):
    c = app_client
    signup(c, "a@acme.io", "Acme")
    ask = c.post("/api/v1/agents", headers=H, json={"name": "Asker", "groups": ["public"],
                                                     "capabilities": ["documents", "data"]}).json()["token"]
    up = c.post("/api/v1/agents", headers=H, json={"name": "Uploader", "groups": ["public"],
                                                    "capabilities": ["documents", "upload"]}).json()["token"]
    adm = c.post("/api/v1/agents", headers=H, json={"name": "Setup", "groups": ["public"], "capabilities": ["documents"],
                                                     "scope": "admin"}).json()
    assert adm["agent"]["scope"] == "admin" and "upload" in adm["agent"]["capabilities"]
    t_ask, t_up, t_adm = tools(c, ask), tools(c, up), tools(c, adm["token"])
    assert "ask" in t_ask and not {"add_document", "create_upload_link", "approve_metric"} & t_ask
    assert "add_document" in t_up and "approve_metric" not in t_up
    assert {"add_document", "approve_metric"} <= t_adm
