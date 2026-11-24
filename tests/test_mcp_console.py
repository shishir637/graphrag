"""Access keys, per-key visibility, the HTTP key gate and the console API. No models needed."""
import asyncio
import json

import pytest

from ui import keys as K
from ui import mcp_server as M

CHUNKS = [
    {"chunk_id": "p1", "source": "handbook.md", "access": ["public"], "headings": ["Leave"], "pages": [],
     "text": "Everyone gets 24 days of annual leave."},
    {"chunk_id": "h1", "source": "salary.md", "access": ["hr"], "headings": ["Bands"], "pages": [],
     "text": "Senior engineer: 15 to 24 lakh."},
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(K, "DATA", tmp_path / "console_data")
    monkeypatch.setattr(K, "KEYS", tmp_path / "console_data" / "keys.json")
    monkeypatch.setattr(K, "CALLS", tmp_path / "console_data" / "mcp_calls.jsonl")
    monkeypatch.setattr(M, "OUT_DIR", tmp_path)
    (tmp_path / "2_chunks.json").write_text(json.dumps(CHUNKS))
    return tmp_path


def test_keys_are_hashed_and_revocable(env):
    rec, token = K.create("Claude Desktop", ["public"])
    assert token.startswith("clk_") and token not in K.KEYS.read_text()     # only the hash is stored
    assert K.verify(token)["name"] == "Claude Desktop"
    assert K.verify("clk_wrong") is None and K.verify(None) is None
    assert K.token_from_header(f"Bearer {token}") == token and K.token_from_header("Basic x") is None
    K.revoke(rec["id"])
    assert K.verify(token) is None


def test_key_needs_a_name(env):
    with pytest.raises(ValueError):
        K.create("   ", ["public"])


def test_each_key_sees_only_its_groups(env):
    pub, _ = K.create("public agent", ["public"])
    hr, _ = K.create("hr agent", ["public", "hr"])
    assert [d["name"] for d in M.run_tool(pub, "list_documents", {})["documents"]] == ["handbook.md"]
    assert [d["name"] for d in M.run_tool(hr, "list_documents", {})["documents"]] == ["handbook.md", "salary.md"]
    assert M.run_tool(pub, "fetch_passage", {"passage_id": "h1"})["found"] is False
    got = M.run_tool(hr, "fetch_passage", {"passage_id": "h1"})
    assert got["found"] and got["text"].startswith("Senior engineer") and got["section"] == "Bands"
    calls = K.recent_calls()
    assert calls[0]["key"] == "hr agent" and calls[0]["summary"] == "found"
    assert K.get(hr["id"])["calls"] == 2


def test_tools_explain_missing_data(env):
    (env / "2_chunks.json").unlink()
    k, _ = K.create("agent", ["public"])
    with pytest.raises(Exception, match="No documents are prepared"):
        M.run_tool(k, "list_documents", {})
    assert K.recent_calls()[0]["ok"] is False


def test_find_entity_without_graph(env):
    k, _ = K.create("agent", ["public"])
    with pytest.raises(Exception, match="not built"):
        M.run_tool(k, "find_entity", {"name": "x"})


def test_gate_refuses_requests_without_a_valid_key(env):
    _, token = K.create("agent", ["public"])
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    gate = M.KeyGate(inner)

    def call(headers):
        out = []

        async def send(msg):
            out.append(msg)

        async def receive():
            return {"type": "http.request", "body": b""}
        asyncio.run(gate({"type": "http", "path": "/mcp", "headers": headers}, receive, send))
        return out[0]["status"], out

    assert call([])[0] == 401
    status, msgs = call([(b"authorization", b"Bearer clk_nope")])
    assert status == 401 and b"invalid_token" in msgs[1]["body"]
    assert call([(b"authorization", f"Bearer {token}".encode())])[0] == 200 and seen == ["/mcp"]


def test_tools_are_registered_read_only():
    tools = {t.name: t for t in M.mcp._tool_manager.list_tools()}
    assert set(tools) == {"list_documents", "search", "ask", "find_entity", "fetch_passage"}
    assert all(t.annotations.read_only_hint for t in tools.values())


def test_console_api(env):
    from fastapi.testclient import TestClient
    import ui.server as server
    with TestClient(server.app) as c:
        r = c.post("/api/console/keys", json={"name": "Cursor", "groups": ["hr"]})
        assert r.status_code == 200 and r.json()["token"].startswith("clk_")
        kid = r.json()["key"]["id"]
        assert "hash" not in json.dumps(c.get("/api/console/keys").json())
        r = c.post("/api/console/try", json={"key_id": kid, "tool": "fetch_passage", "args": {"passage_id": "h1"}})
        assert r.json()["result"]["found"] is True
        assert c.post("/api/console/try", json={"key_id": kid, "tool": "nope", "args": {}}).status_code == 404
        assert c.post(f"/api/console/keys/{kid}/revoke").json()["key"]["revoked"] is True
        assert c.post("/api/console/try", json={"key_id": kid, "tool": "list_documents"}).status_code == 400
        assert c.post("/mcp", json={}).status_code == 401
        assert c.get("/console").status_code == 200
