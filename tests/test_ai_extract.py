"""Optional AI extraction (graph mode "ai"): providers, parsing, cache, fallback, settings API.
A small fake HTTP server stands in for Claude / OpenAI-compatible / Ollama, so no key or network is needed."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from graph import extract_ai as A
from graph import nlp_extract as N

REPLY = {"entities": [{"name": "Maya Rao", "type": "Person", "description": "Maya Rao heads Grid Analytics."},
                      {"name": "Grid Analytics", "type": "organisation", "description": "A department."},
                      {"name": "Pune", "type": "City", "description": "A city."},
                      {"name": "42 customers", "type": "Number", "description": "not a schema type"}],
         "relations": [{"source": "Maya Rao", "type": "LEADS", "target": "Grid Analytics", "description": "Maya Rao heads Grid Analytics.", "strength": 9},
                       {"source": "Grid Analytics", "type": "located_in", "target": "Pune", "description": "It is in Pune.", "strength": "x"},
                       {"source": "Grid Analytics", "type": "OWNS_SOMETHING", "target": "Pune", "description": "unknown type"},
                       {"source": "Nobody", "type": "LEADS", "target": "Pune", "description": "unknown entity"}]}


class Fake:
    calls: list = []
    status = 200
    fence = False


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        Fake.calls.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        if Fake.status != 200:
            self.send_response(Fake.status); self.end_headers(); self.wfile.write(b'{"error":"busy"}'); return
        text = json.dumps(REPLY)
        if Fake.fence:
            text = "Here you go:\n```json\n" + text + "\n```"
        if self.path.endswith("/v1/messages"):
            out = {"content": [{"type": "text", "text": text}], "usage": {"input_tokens": 100, "output_tokens": 50}}
        else:
            out = {"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": 90, "completion_tokens": 40}}
        data = json.dumps(out).encode()
        self.send_response(200); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data))); self.end_headers(); self.wfile.write(data)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    Fake.calls, Fake.status, Fake.fence = [], 200, False
    monkeypatch.setattr(A, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    for k in ("CL_AI_PROVIDER", "CL_AI_MODEL", "CL_AI_BASE_URL", "CL_AI_WORKERS", A.KEY_ENV):
        monkeypatch.delenv(k, raising=False)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def cfg(base, provider="local", key=""):
    return {"provider": provider, "model": "test-model", "base_url": base + ("" if provider == "anthropic" else "/v1"),
            "workers": 4, "api_key": key}


def chunks(tmp_path, monkeypatch, n=3):
    cf = tmp_path / "2_chunks.json"
    cf.write_text(json.dumps([{"chunk_id": f"c{i}", "text": f"Maya Rao heads Grid Analytics in Pune. ({i})",
                               "headings": ["Teams"], "source": "x.md"} for i in range(n)]))
    monkeypatch.setattr(N, "CHUNKS", cf)


def test_normalise_keeps_schema_types_and_valid_links(fake):
    ex = A.AIExtractor(cfg(fake))
    ents, rels = ex.normalise(REPLY)
    assert {e["name"] for e in ents} == {"Maya Rao", "Grid Analytics", "Pune"}             # "Number" is not a type
    assert next(e for e in ents if e["name"] == "Grid Analytics")["type"] == "Organisation"  # case-insensitive type
    got = {(r["source"], r["type"], r["target"]) for r in rels}
    assert got == {("Maya Rao", "LEADS", "Grid Analytics"), ("Grid Analytics", "LOCATED_IN", "Pune"),
                   ("Grid Analytics", "RELATED_TO", "Pune")}                                 # unknown type -> RELATED_TO
    assert all(1 <= r["strength"] <= 10 for r in rels)


def test_local_provider_parallel_cached_and_usage(fake, tmp_path, monkeypatch):
    chunks(tmp_path, monkeypatch)
    monkeypatch.setattr(A, "config", lambda: cfg(fake))
    out = tmp_path / "gr_extract_ai.jsonl"
    N.extract_all(out, mode="ai", resume=False)
    recs = [json.loads(x) for x in out.read_text().splitlines()]
    assert [r["chunk_id"] for r in recs] == ["c0", "c1", "c2"] and all(r["th"] for r in recs)
    assert recs[0]["method"] == "ai-local:test-model"
    assert any(r["type"] == "LEADS" for r in recs[0]["relations"])
    path, headers, body = Fake.calls[0]
    assert path == "/v1/chat/completions" and "authorization" not in headers  # local: no key sent
    assert body["response_format"] == {"type": "json_object"} and "<passage>" in body["messages"][1]["content"]
    usage = json.loads((tmp_path / "gr_ai_usage.json").read_text())
    assert usage["calls"] == 3 and usage["input_tokens"] == 270 and usage["destination"].startswith("127.0.0.1")
    # rebuild from scratch: every passage comes from the cache, no new calls
    n = len(Fake.calls)
    N.extract_all(out, mode="ai", resume=False)
    assert len(Fake.calls) == n
    assert json.loads((tmp_path / "gr_ai_usage.json").read_text())["from_cache"] == 3


def test_anthropic_provider_and_fenced_reply(fake, tmp_path):
    Fake.fence = True
    ex = A.AIExtractor(cfg(fake, "anthropic", key="sk-test"))
    rec = ex.run({"chunk_id": "c9", "text": "Maya Rao heads Grid Analytics.", "headings": []})
    path, headers, body = Fake.calls[0]
    assert path == "/v1/messages" and headers.get("x-api-key") == "sk-test" and body["model"] == "test-model"
    assert len(rec["entities"]) == 3 and ex.usage["input_tokens"] == 100


def test_failed_calls_fall_back_and_build_stops_if_all_fail(fake, tmp_path, monkeypatch):
    Fake.status = 503
    chunks(tmp_path, monkeypatch)
    monkeypatch.setattr(A, "config", lambda: cfg(fake))
    monkeypatch.setattr(A.AIExtractor, "_fallback_run",
                        lambda self, c: {"chunk_id": c["chunk_id"], "method": "fallback", "entities": [], "relations": []})
    with pytest.raises(RuntimeError, match="Every AI call failed"):
        N.extract_all(tmp_path / "o.jsonl", mode="ai", resume=False)
    assert len(Fake.calls) == 3 * A.RETRIES                      # retried, then fell back per passage
    assert not list((tmp_path / "cache").glob("*.json"))        # failures are not cached


def test_problems_and_local_detection():
    assert "API key" in " ".join(A.problems({"provider": "anthropic", "model": "m", "base_url": "https://api.anthropic.com", "api_key": ""}))
    assert A.problems({"provider": "local", "model": "m", "base_url": "http://host.docker.internal:11434/v1", "api_key": ""}) == []
    assert "model" in " ".join(A.problems({"provider": "openai", "model": "", "base_url": "https://x.io/v1", "api_key": "k"}))
    assert A.is_local({"base_url": "http://192.168.1.5:11434/v1"}) and not A.is_local({"base_url": "https://api.openai.com/v1"})


def test_settings_api(fake, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import core, engine
    core.reset_for_tests(tmp_path / "var")
    monkeypatch.setattr(engine, "OUT_DIR", tmp_path)
    monkeypatch.setattr(engine, "postgres_ok", lambda: (False, "test"))
    monkeypatch.setenv("CL_PUBLIC_HOSTS", "testserver")
    from app.main import asgi
    hd = {"X-CL": "1"}
    with TestClient(asgi) as c:
        assert c.post("/api/v1/setup", headers=hd, json={"name": "O", "email": "o@example.com", "password": "pass-word-123"}).status_code == 200
        s = c.get("/api/v1/settings").json()
        assert s["graph_mode"] == "accurate" and s["ai_extraction"]["ready"] is False and s["ai_model"]["inside"] is False
        assert c.put("/api/v1/settings/graph", headers=hd, json={"mode": "ai"}).status_code == 400   # not set up yet
        r = c.put("/api/v1/settings/ai", headers=hd, json={"provider": "anthropic", "model": "", "base_url": fake, "api_key": "sk-ant-secret-1234"})
        assert r.status_code == 200
        ai = c.get("/api/v1/settings").json()["ai_extraction"]
        assert ai["ready"] and ai["model"] == "claude-haiku-5-5" and ai["key_set"] and "secret" not in json.dumps(ai)
        assert ai["local"] is True                                   # 127.0.0.1
        t = c.post("/api/v1/settings/ai/test", headers=hd).json()
        assert t["ok"] and t["entities"] == 4
        assert c.put("/api/v1/settings/graph", headers=hd, json={"mode": "ai"}).status_code == 200
        s = c.get("/api/v1/settings").json()
        assert s["graph_mode"] == "ai" and s["ai_model"]["inside"] is True
        # saving without api_key keeps the key; "" removes it
        c.put("/api/v1/settings/ai", headers=hd, json={"provider": "anthropic", "model": "m2", "base_url": fake})
        assert c.get("/api/v1/settings").json()["ai_extraction"]["key_set"]
        c.put("/api/v1/settings/ai", headers=hd, json={"provider": "anthropic", "model": "m2", "base_url": fake, "api_key": ""})
        assert not c.get("/api/v1/settings").json()["ai_extraction"]["key_set"]
        assert c.put("/api/v1/settings/ai", headers=hd, json={"provider": "x"}).status_code == 400
        events = json.dumps(c.get("/api/v1/events").json())
        assert "settings.ai_extraction" in events and "sk-ant-secret" not in events
    monkeypatch.delenv(A.KEY_ENV, raising=False)
