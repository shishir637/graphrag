"""Sources (GitHub, databases, S3 folders) kept up to date on their own: encrypted secrets, a GitHub sync against a
simulated GitHub API (no network) whose files go into the chosen data set and whose activity becomes tables with a
ready-made engineering layer that compiles, incremental syncs and removals, signed webhooks, the scheduler, the
admin API, and an S3 folder through a stand-in client. No models needed."""
import base64
import hashlib
import hmac
import json
from contextlib import nullcontext

import httpx
import pytest
from fastapi.testclient import TestClient

from app import core, engine, jobs, scheduler, vault
from semantic import config as SC
from semantic import defstore
from sources import github as G
from sources import manage
from tests.test_app import H, setup_owner


class FakeGitHub:
    def __init__(self):
        self.files = {"README.md": ("s1", "# Orion API\nThe Orion API serves field-service bookings."),
                      "LICENSE": ("s5", "MIT License"),
                      "src/app.py": ("s2", "def book(slot):\n    return slot\n"),
                      "node_modules/x/index.js": ("s3", "junk"), "logo.png": ("s4", "png")}
        self.calls = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path
        self.calls.append(p)
        repo = "/repos/acme/orion"
        if p == repo:
            return httpx.Response(200, json={"name": "orion", "default_branch": "main", "private": True, "language": "Python",
                                             "stargazers_count": 3, "forks_count": 1, "created_at": "2025-01-01T00:00:00Z",
                                             "pushed_at": "2026-11-20T00:00:00Z"})
        if p == f"{repo}/git/trees/main":
            return httpx.Response(200, json={"tree": [{"path": k, "type": "blob", "sha": v[0], "size": len(v[1])}
                                                      for k, v in self.files.items()]})
        if p.startswith(f"{repo}/git/blobs/"):
            sha = p.rsplit("/", 1)[1]
            text = next(v[1] for v in self.files.values() if v[0] == sha)
            return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(text.encode()).decode()})
        if p == f"{repo}/commits":
            return httpx.Response(200, json=[{"sha": "c1", "author": {"login": "ann"},
                                              "commit": {"author": {"name": "Ann", "date": "2026-11-01T10:00:00Z"}}}])
        if p == f"{repo}/pulls":
            return httpx.Response(200, json=[
                {"number": 7, "title": "Add booking", "user": {"login": "ann"}, "state": "closed",
                 "created_at": "2026-11-01T00:00:00Z", "merged_at": "2026-11-01T10:00:00Z", "closed_at": "2026-11-01T10:00:00Z",
                 "updated_at": "2099-01-01T00:00:00Z"},
                {"number": 8, "title": "WIP", "user": {"login": "ben"}, "state": "open", "created_at": "2026-11-03T00:00:00Z",
                 "merged_at": None, "closed_at": None, "updated_at": "2099-01-01T00:00:00Z"}])
        if p == f"{repo}/issues":
            return httpx.Response(200, json=[
                {"number": 9, "title": "Crash on save", "user": {"login": "cy"}, "state": "open", "labels": [{"name": "bug"}],
                 "created_at": "2026-11-04T00:00:00Z", "closed_at": None}])
        if p == f"{repo}/actions/runs":
            return httpx.Response(200, json={"workflow_runs": [
                {"id": 1, "name": "CI", "event": "push", "head_branch": "main", "status": "completed", "conclusion": "success",
                 "created_at": "2026-11-01T00:00:00Z", "run_started_at": "2026-11-01T00:00:00Z", "updated_at": "2026-11-01T00:06:00Z"}]})
        if p == f"{repo}/contributors":
            return httpx.Response(200, json=[{"login": "ann", "contributions": 30}])
        return httpx.Response(404, json={})


@pytest.fixture
def env(tmp_path, monkeypatch):
    core.reset_for_tests(tmp_path / "var")
    vault.reset_for_tests()
    manage._ready = None
    docs, out, data = tmp_path / "documents", tmp_path / "outputs", tmp_path / "data"
    docs.mkdir(); out.mkdir(); data.mkdir()
    monkeypatch.setattr(engine, "DOCS_DIR", docs)
    monkeypatch.setattr(engine, "ACCESS_FILE", docs / "_access.json")
    monkeypatch.setattr(engine, "OUT_DIR", out)
    monkeypatch.setattr(SC, "DATA_DIR", data)
    monkeypatch.setattr(engine, "postgres_ok", lambda: (False, "test"))
    monkeypatch.setattr(engine, "graph_ready", lambda: False)
    rebuilt = []
    monkeypatch.setattr(engine, "rebuild", lambda info: rebuilt.append(info))
    monkeypatch.setenv("CL_DEFS_DSN", f"sqlite:///{tmp_path / 'defs.db'}")
    monkeypatch.setenv("CL_DEFS_EXPORT", "0")
    defstore.reset_for_tests()
    fake = FakeGitHub()
    monkeypatch.setattr(G.GitHub, "_client",
                        lambda self: httpx.Client(base_url=G.API, transport=httpx.MockTransport(fake.handler)))
    yield fake, rebuilt, docs, data
    defstore.reset_for_tests()


def test_secrets_are_encrypted(env):
    vault.put("source:x:token", "ghp_supersecret_123456")
    assert vault.get("source:x:token") == "ghp_supersecret_123456"
    raw = core.db().execute("SELECT ciphertext, hint FROM secrets").fetchone()
    assert "supersecret" not in raw["ciphertext"] and raw["hint"].startswith("ghp_sup")
    vault.put("source:x:token", None)
    assert vault.get("source:x:token") is None


def test_github_sync_into_a_data_set(env):
    from app import access
    from semantic.db import columns  # noqa: F401
    fake, rebuilt, docs, data = env
    s = manage.create("github", "Orion", {"repos": ["acme/orion"]}, {"token": "ghp_secret_value_123456"}, None, 60, "o@example.com")
    assert s["dataset"] == "orion" and "orion" in {d["name"] for d in access.datasets()}   # a new data set
    assert s["secrets"]["token"] == core.mask("ghp_secret_value_123456")       # only a hint
    assert "secret_value" not in json.dumps(manage.list_all())                # secrets never come back
    assert manage.test(s["id"])["ok"]
    manage.run_sync({"source": s["id"]})
    amap = engine.access_map()
    names = {G._doc_name("acme/orion", p) for p in ("README.md", "src/app.py", "LICENSE")}
    assert set(amap) == names and all(v == ["orion"] for v in amap.values())
    assert "```python" in (docs / G._doc_name("acme/orion", "src/app.py")).read_text()
    assert (data / "github_pull_requests.parquet").exists()
    assert {f["kind"] for f in rebuilt[-1]["files"]} == {"document", "table"}
    got = manage.get(s["id"])
    assert got["documents"] == 3 and got["last_ok"] and got["summary"]["files_changed"] == 3
    # the ready-made layer went into the company layer and its metrics compile on the synced tables
    from semantic import db as sdb
    from semantic.metrics import validate
    from semantic.schema import MetricSet, SemanticModel
    from semantic.yamlio import load_metrics, load_model
    m, ms = load_model(), load_metrics()
    gm = SemanticModel.model_validate(m.model_dump() | {"entities": [e for e in m.model_dump()["entities"]
                                                                       if e["name"].startswith("github_")],
                                                          "joins": [j for j in m.model_dump().get("joins", [])
                                                                    if j["from_entity"].startswith("github_")]})
    gms = MetricSet.model_validate(ms.model_dump() | {"metrics": [x for x in ms.model_dump()["metrics"]
                                                                   if x["entity"].startswith("github_")]})
    assert any(x.name == "median_hours_to_merge" for x in gms.metrics)
    sdb_dir = sdb.DATA_DIR
    try:
        sdb.DATA_DIR = data
        assert validate(gm, gms, sdb.connect()) == []
    finally:
        sdb.DATA_DIR = sdb_dir


def test_incremental_sync_and_removal(env):
    fake, rebuilt, docs, _ = env
    s = manage.create("github", "Orion", {"repos": ["acme/orion"], "include_activity": False}, {}, "public", 60, "o")
    manage.run_sync({"source": s["id"]})
    fake.calls.clear()
    fake.files["README.md"] = ("s1b", "# Orion API v2")
    del fake.files["LICENSE"]
    manage.run_sync({"source": s["id"]})
    blobs = [c for c in fake.calls if "/git/blobs/" in c]
    assert blobs == ["/repos/acme/orion/git/blobs/s1b"]                     # only the changed file is fetched
    assert not (docs / G._doc_name("acme/orion", "LICENSE")).exists()
    assert manage.get(s["id"])["documents"] == 2
    fake.calls.clear()
    rebuilt.clear()
    manage.run_sync({"source": s["id"]})
    assert rebuilt == []                                                      # nothing changed: no rebuild
    out = manage.delete(s["id"], "o")
    assert out["removed_documents"] == 2 and engine.access_map() == {}


def test_moving_a_source_to_another_data_set(env, monkeypatch):
    queued = []
    monkeypatch.setattr(engine, "queue_rebuild", lambda saved, by, **k: queued.append(saved))
    s = manage.create("github", "Orion", {"repos": ["acme/orion"], "include_activity": False}, {}, "public", 60, "o")
    manage.run_sync({"source": s["id"]})
    manage.update(s["id"], dataset="engineering", by="o")
    assert set(map(tuple, engine.access_map().values())) == {("engineering",)}
    assert queued and manage.get(s["id"])["dataset"] == "engineering"


def test_webhook_signature_and_schedule(env, monkeypatch):
    queued = []
    monkeypatch.setattr(manage, "queue_sync", lambda sid, by: queued.append((sid, by)))
    s = manage.create("github", "Orion", {"repos": ["acme/orion"]}, {}, "public", 60, "o")
    body = b'{"ref": "refs/heads/main"}'
    assert manage.webhook("github", s["id"], body, "", "push")[0] == 403        # no secret yet
    manage.update(s["id"], secrets={"webhook_secret": "whsec-123"})
    assert manage.webhook("github", s["id"], body, "sha256=bad", "push")[0] == 401
    sig = "sha256=" + hmac.new(b"whsec-123", body, hashlib.sha256).hexdigest()
    assert manage.webhook("github", s["id"], body, sig, "ping") == (200, "pong")
    assert manage.webhook("github", s["id"], body, sig, "push")[0] == 202
    assert manage.webhook("s3", s["id"], body, sig, "push")[0] == 404
    assert queued == [(s["id"], "webhook")]
    # the scheduler queues a due source once, then moves next_run on
    queued.clear()
    assert scheduler.tick() == 1 and queued == [(s["id"], "schedule")]
    assert scheduler.tick() == 0
    manage.update(s["id"], status="paused")
    assert manage.webhook("github", s["id"], body, sig, "push") == (202, "source paused")


def test_s3_folder_with_a_stand_in_client(env):
    from sources.s3 import S3Folder

    class FakeS3:
        objects = {"contracts/a.md": (b"# Contract A", '"e1"'), "contracts/b.txt": (b"Terms B", '"e2"'),
                   "contracts/skip.bin": (b"\x00", '"e3"')}

        def list_objects_v2(self, Bucket, Prefix="", **kw):
            return {"Contents": [{"Key": k, "ETag": v[1], "Size": len(v[0])} for k, v in self.objects.items()
                                 if k.startswith(Prefix)], "IsTruncated": False}

        def get_object(self, Bucket, Key):
            import io
            return {"Body": io.BytesIO(self.objects[Key][0])}

    fake = FakeS3()
    orig = S3Folder.client
    S3Folder.client = lambda self: nullcontext(fake)
    try:
        s = manage.create("s3", "Contracts", {"bucket": "legal-docs", "prefix": "contracts/"}, {}, "legal", 60, "o")
        manage.run_sync({"source": s["id"]})
    finally:
        S3Folder.client = orig
    assert manage.get(s["id"])["documents"] == 2
    assert all(v == ["legal"] for v in engine.access_map().values())


def test_sources_api(env, monkeypatch):
    monkeypatch.setenv("CL_PUBLIC_HOSTS", "testserver")
    queued = []
    monkeypatch.setattr(manage, "queue_sync", lambda sid, by: queued.append(sid) or {"id": "j1"})
    from app.main import asgi
    with TestClient(asgi) as c:
        setup_owner(c)
        assert {k["kind"] for k in c.get("/api/v1/sources").json()["kinds"]} >= {"github", "postgres"}
        r = c.post("/api/v1/sources", headers=H, json={"kind": "github", "name": "Orion",
                                                       "config": {"repos": ["acme/orion"]},
                                                       "secrets": {"token": "ghp_abcdefghijkl"}})
        assert r.status_code == 200, r.text
        sid = r.json()["id"]
        assert "ghp_abcdefghijkl" not in r.text and r.json()["webhook"] == f"/hooks/github/{sid}"
        assert c.post("/api/v1/sources", headers=H, json={"kind": "ftp"}).status_code == 400
        assert c.post(f"/api/v1/sources/{sid}/test", headers=H).json()["ok"]
        assert c.post(f"/api/v1/sources/{sid}/sync", headers=H).status_code == 200 and queued == [sid]
        assert c.get("/api/v1/data").json()["sources"][0]["id"] == sid          # shown on the Data tab
        assert c.patch(f"/api/v1/sources/{sid}", headers=H, json={"status": "paused"}).json()["status"] == "paused"
        assert c.post(f"/hooks/github/{sid}", content=b"{}", headers={"x-hub-signature-256": "sha256=x"}).status_code == 403
        assert c.delete(f"/api/v1/sources/{sid}", headers=H).status_code == 200
        assert c.get("/api/v1/sources").json()["sources"] == []
        acts = [e["action"] for e in core.list_events()]
        assert "source.create" in acts and "source.delete" in acts
