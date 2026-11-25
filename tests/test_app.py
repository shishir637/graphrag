"""The Context Layer app: setup, sign-in, roles, CSRF guard, agents, MCP key gate, capabilities, audit.
No models or AI key needed."""
import json

import pytest
from fastapi.testclient import TestClient

from app import core, engine
from app import mcp_server as M

H = {"X-CL": "1"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    core.reset_for_tests(tmp_path / "var")
    docs, out = tmp_path / "documents", tmp_path / "outputs"
    docs.mkdir(); out.mkdir()
    monkeypatch.setattr(engine, "DOCS_DIR", docs)
    monkeypatch.setattr(engine, "ACCESS_FILE", docs / "_access.json")
    monkeypatch.setattr(engine, "OUT_DIR", out)
    monkeypatch.setattr(engine, "postgres_ok", lambda: (False, "test"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CL_PUBLIC_HOSTS", "testserver")
    from app.main import asgi
    with TestClient(asgi) as c:
        yield c, tmp_path


def setup_owner(c):
    r = c.post("/api/v1/setup", headers=H, json={"name": "Owner", "email": "o@example.com", "password": "pass-word-123", "organisation": "Acme"})
    assert r.status_code == 200, r.text


def test_setup_once_and_login(client):
    c, _ = client
    assert c.get("/api/v1/setup").json()["needs_setup"] is True
    assert c.post("/api/v1/setup", json={}).status_code == 403                      # no X-CL header
    assert c.post("/api/v1/setup", headers=H, json={"name": "O", "email": "o@example.com", "password": "short"}).status_code == 400
    setup_owner(c)
    assert c.post("/api/v1/setup", headers=H, json={"name": "X", "email": "x@example.com", "password": "pass-word-123"}).status_code == 409
    assert c.get("/api/v1/me").json()["user"]["role"] == "owner"
    c.post("/api/v1/logout", headers=H)
    assert c.get("/api/v1/me").status_code == 401
    assert c.post("/api/v1/login", headers=H, json={"email": "o@example.com", "password": "wrong-password"}).status_code == 401
    assert c.post("/api/v1/login", headers=H, json={"email": "O@Example.com", "password": "pass-word-123"}).status_code == 200


def test_roles(client):
    c, _ = client
    setup_owner(c)
    assert c.post("/api/v1/users", headers=H, json={"email": "v@example.com", "name": "V", "role": "viewer", "password": "viewer-pass-1"}).status_code == 200
    assert c.patch("/api/v1/users/1", headers=H, json={"role": "admin"}).status_code == 400   # last owner
    c.post("/api/v1/logout", headers=H)
    c.post("/api/v1/login", headers=H, json={"email": "v@example.com", "password": "viewer-pass-1"})
    assert c.get("/api/v1/agents").status_code == 200
    assert c.post("/api/v1/agents", headers=H, json={"name": "x"}).status_code == 403
    assert c.post("/api/v1/jobs/documents", headers=H).status_code == 403
    assert c.put("/api/v1/settings/graph", headers=H, json={"mode": "fast"}).status_code == 403
    assert "ai_model" in c.get("/api/v1/settings").json() and c.get("/api/v1/settings").json()["ai_model"]["inside"] is False


def test_documents_and_groups(client):
    c, tmp = client
    setup_owner(c)
    r = c.post("/api/v1/documents", headers=H, files=[("files", ("leave.txt", b"24 days of leave."))], data={"groups": "public"})
    assert r.json()["saved"] == [{"name": "leave.txt", "groups": ["public"]}]
    r = c.post("/api/v1/documents", headers=H, files=[("files", ("evil.exe", b"x"))], data={"groups": "public"})
    assert r.status_code == 400
    assert c.patch("/api/v1/documents/leave.txt", headers=H, json={"groups": ["Bad Group!"]}).status_code == 400
    assert c.patch("/api/v1/documents/leave.txt", headers=H, json={"groups": ["hr", "public"]}).json()["groups"] == ["hr", "public"]
    docs = c.get("/api/v1/documents").json()
    assert docs["documents"][0]["status"] == "new" and docs["groups"] == ["hr", "public"]
    assert json.loads((tmp / "documents" / "_access.json").read_text()) == {"leave.txt": ["hr", "public"]}
    assert c.delete("/api/v1/documents/leave.txt", headers=H).status_code == 200
    assert not (tmp / "documents" / "leave.txt").exists()


def test_agents_and_mcp(client):
    c, _ = client
    setup_owner(c)
    assert c.post("/api/v1/agents", headers=H, json={"name": "x", "capabilities": []}).status_code == 400
    r = c.post("/api/v1/agents", headers=H, json={"name": "Support bot", "groups": ["public"], "capabilities": ["documents"]}).json()
    token, aid = r["token"], r["agent"]["id"]
    assert token.startswith("clk_")
    with core.system():                                   # only a hash of the key is stored
        stored = [str(v) for r in core.db().execute("SELECT * FROM agents") for v in r]
    assert stored and not any(token in v for v in stored)
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    hdr = {"accept": "application/json, text/event-stream"}
    m = c
    if True:
        assert m.post("/mcp", json=init, headers=hdr).status_code == 401
        assert m.post("/mcp", json=init, headers=hdr | {"authorization": f"Bearer {token}"}).status_code == 200

        def call(name, args):
            body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args}}
            return m.post("/mcp", json=body, headers=hdr | {"authorization": f"Bearer {token}"}).json()["result"]
        res = call("query_metrics", {"question": "revenue"})
        assert res["isError"] and "may not use data" in res["content"][0]["text"]
        res = call("list_documents", {})
        assert not res.get("isError") and res["structuredContent"]["your_groups"] == ["public"]
        res = call("ask", {"question": "anything"})
        assert "AI model" not in json.dumps(res) and "API key" not in json.dumps(res)
        res = call("describe_data", {})
        assert res["isError"] and "may not use data" in res["content"][0]["text"]
        c.post(f"/api/v1/agents/{aid}/revoke", headers=H)
        assert m.post("/mcp", json=init, headers=hdr | {"authorization": f"Bearer {token}"}).status_code == 401
    ev = c.get("/api/v1/events?type=agent").json()["events"]
    assert {e["action"] for e in ev} >= {"tool.query_metrics", "tool.list_documents", "tool.ask"}
    assert any(not e["ok"] for e in ev)
    users_ev = [e["action"] for e in c.get("/api/v1/events?type=user").json()["events"]]
    assert "agents.create" in users_ev and "agents.revoke" in users_ev
    csv = c.get("/api/v1/events.csv").text
    assert csv.startswith("time,actor_type") and "tool.ask" in csv


def test_public_hosts(monkeypatch):
    monkeypatch.setenv("CL_PUBLIC_HOSTS", "contextlayer.acme.com, 10.0.0.5")
    ts = M.transport_security()
    assert "contextlayer.acme.com" in ts.allowed_hosts and "10.0.0.5:*" in ts.allowed_hosts and "localhost:*" in ts.allowed_hosts
    assert "https://contextlayer.acme.com" in ts.allowed_origins


def test_tools_registered_read_only():
    tools = {t.name: t for t in M.mcp._tool_manager.list_tools()}
    read = {"ask", "search_documents", "describe_data", "query_metrics", "list_metrics", "find_connections",
            "explore_entity", "list_documents", "fetch_passage", "build_status"}
    writes = {"add_document", "create_upload_link", "approve_metric"}
    assert set(tools) == read | writes
    assert all(tools[t].annotations.read_only_hint for t in read)
    assert not any(tools[t].annotations.read_only_hint or tools[t].annotations.destructive_hint for t in writes)


def test_password_hashing():
    h = core.hash_password("pass-word-123")
    assert core.check_password("pass-word-123", h) and not core.check_password("other", h)
    assert core.check_password("x", "garbage") is False


def test_metrics_over_mcp_without_ai(client):
    """A data-only key: describe_data, a structured request filled in by the agent, and a plain question."""
    c, _ = client
    setup_owner(c)
    token = c.post("/api/v1/agents", headers=H, json={"name": "Finance bot", "groups": ["public"],
                                                     "capabilities": ["data"]}).json()["token"]
    hdr = {"accept": "application/json, text/event-stream", "authorization": f"Bearer {token}"}

    def call(name, args):
        body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args}}
        return c.post("/mcp", json=body, headers=hdr).json()["result"]
    d = call("describe_data", {})["structuredContent"]
    assert "net_revenue" in {m["name"] for m in d["metrics"]} and d["business_rules"]
    r = call("query_metrics", {"metrics": ["net_revenue"], "dimensions": ["regions.region_name"], "period": "FY2025-26"})
    sc = r["structuredContent"]
    assert not r.get("isError") and sc["row_count"] >= 4 and "SELECT" in sc["sql"].upper()
    r = call("query_metrics", {"question": "net revenu in Bangalor last quarter"})["structuredContent"]
    assert r["found"] and r["planned_by"] == "rule planner" and any("Bengaluru" in str(f) for f in r["request"]["filters"])
    r = call("query_metrics", {"question": "What is our profit margin?"})["structuredContent"]
    assert r["found"] is False and "Available:" in r["not_answerable"]
    r = call("query_metrics", {"metrics": ["no_such_metric"]})
    assert r["isError"]


def test_public_host_serves_only_mcp(client, monkeypatch):
    """Through a tunnel hostname only /mcp and the health check answer; the admin app stays local."""
    c, _ = client
    monkeypatch.setenv("CL_PUBLIC_HOSTS", "*.trycloudflare.com")
    pub = {"host": "abc-def.trycloudflare.com"}
    assert c.get("/api/v1/health", headers=pub).status_code == 200
    assert c.get("/api/v1/setup", headers=pub).status_code == 404
    assert c.get("/", headers=pub).status_code == 404
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    r = c.post("/mcp", json=init, headers=pub | {"accept": "application/json, text/event-stream"})
    assert r.status_code == 401                                          # reaches MCP; needs a key
    assert c.get("/api/v1/setup", headers={"host": "evil.example.com"}).status_code == 421
    assert c.get("/api/v1/setup", headers={"host": "localhost:8080"}).status_code == 200
