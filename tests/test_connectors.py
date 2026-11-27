"""Connectors: encrypted secrets, the GitHub connector against a simulated GitHub API (no network): code and docs
become documents with one access group per repository, activity becomes tables with the ready-made engineering
layer whose metrics all run, syncs are incremental, row access rules, webhooks, scheduling and the admin tools."""
import base64
import hashlib
import hmac
import json

import httpx
import pytest

from app import core, jobs, scheduler, vault
from connectors import github as G
from connectors import manage
from tests.conftest import needs_pg

pytestmark = needs_pg


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
                                              "commit": {"author": {"name": "Ann", "date": "2026-11-01T10:00:00Z"}}},
                                             {"sha": "c2", "author": {"login": "ben"},
                                              "commit": {"author": {"name": "Ben", "date": "2026-11-02T10:00:00Z"}}}])
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
                 "created_at": "2026-11-04T00:00:00Z", "closed_at": None},
                {"number": 8, "pull_request": {}, "title": "WIP", "user": {"login": "ben"}, "state": "open", "labels": []}])
        if p == f"{repo}/actions/runs":
            return httpx.Response(200, json={"workflow_runs": [
                {"id": 1, "name": "CI", "event": "push", "head_branch": "main", "status": "completed", "conclusion": "success",
                 "created_at": "2026-11-01T00:00:00Z", "run_started_at": "2026-11-01T00:00:00Z", "updated_at": "2026-11-01T00:06:00Z"},
                {"id": 2, "name": "CI", "event": "push", "head_branch": "main", "status": "completed", "conclusion": "failure",
                 "created_at": "2026-11-02T00:00:00Z", "run_started_at": "2026-11-02T00:00:00Z", "updated_at": "2026-11-02T00:04:00Z"}]})
        if p == f"{repo}/contributors":
            return httpx.Response(200, json=[{"login": "ann", "contributions": 30}, {"login": "ben", "contributions": 12}])
        return httpx.Response(404, json={})


@pytest.fixture
def ws(tmp_path, monkeypatch):
    jobs.stop_workers()
    core.reset_for_tests(tmp_path / "var")
    vault.reset_for_tests()
    fake = FakeGitHub()
    monkeypatch.setattr(G.GitHub, "_client", lambda self: httpx.Client(base_url=G.API, transport=httpx.MockTransport(fake.handler)))
    rebuilt = []
    from app import engine
    monkeypatch.setattr(engine, "rebuild", lambda info: rebuilt.append(info))
    monkeypatch.setattr(engine, "graph_ready", lambda: False)
    return core.create_tenant("Acme"), core.create_tenant("Globex"), fake, rebuilt


def test_secrets_are_encrypted_and_per_workspace(ws):
    a, b, _, _ = ws
    with core.tenant_scope(a):
        vault.put("connector:x:token", "ghp_supersecret_123456")
        assert vault.get("connector:x:token") == "ghp_supersecret_123456"
        raw = core.db().execute("SELECT ciphertext, hint FROM secrets").fetchone()
        assert "supersecret" not in raw["ciphertext"] and raw["hint"].startswith("ghp_sup")
    with core.tenant_scope(b):
        assert vault.get("connector:x:token") is None


def test_github_sync_documents_tables_layer_and_access(ws):
    from rag import registry
    from rag.config import DOCS_DIR
    from semantic import access, repo
    from semantic.config import DATA_DIR
    a, b, fake, rebuilt = ws
    with core.tenant_scope(a):
        c = manage.create("github", "Orion", {"repos": ["acme/orion"]}, {"token": "ghp_x"}, None, 60, "ann@acme.io")
        assert manage.test(c["id"])["ok"]
        manage.run_sync({"connector": c["id"]})
        group = G.repo_group("acme/orion")
        docs = registry.access_map()
        assert set(docs) == {G._doc_name("acme/orion", "README.md"), G._doc_name("acme/orion", "src/app.py"),
                             G._doc_name("acme/orion", "LICENSE")}                  # extensionless well-known files too
        assert all(g == [group] for g in docs.values())                       # one access group per repository
        assert "```python" in (DOCS_DIR / G._doc_name("acme/orion", "src/app.py")).read_text()
        assert (DATA_DIR / "github_pull_requests.parquet").exists()                # landed as Parquet
        assert repo.get("metric", "median_hours_to_merge")["status"] == "approved"   # ready-made layer, approved
        assert {(r["entity"], r["group"]) for r in access.rules()} >= {("github_commits", group)}
        kinds = {f["kind"] for f in rebuilt[-1]["files"]}
        assert kinds == {"document", "table"}
        # every metric of the template runs on the synced tables
        from semantic import monitor
        out = monitor.run()
        assert out["checked"] >= 19 and out["failed"] == []
        # the numbers: 1 of 2 PRs merged after 10 hours, 1 of 2 builds failed
        from semantic.compiler import run as run_q
        from semantic.schema import SemanticQuery
        _, df = run_q(SemanticQuery(metrics=["pull_requests_merged", "median_hours_to_merge"]))
        assert int(df.iloc[0, 0]) == 1 and float(df.iloc[0, 1]) == 10.0
        _, df = run_q(SemanticQuery(metrics=["build_failure_rate"]))
        assert float(df.iloc[0, 0]) == 0.5

        # second sync: nothing changed -> no documents fetched again, no rebuild of documents
        n_blobs = sum("/git/blobs/" in p for p in fake.calls)
        rebuilt.clear()
        manage.run_sync({"connector": c["id"]})
        assert sum("/git/blobs/" in p for p in fake.calls) == n_blobs
        assert not any(f["kind"] == "document" for r in rebuilt for f in r["files"])
        # one file edited, one removed -> only those change
        fake.files["src/app.py"] = ("s2b", "def book(slot, who):\n    return slot\n")
        del fake.files["README.md"]
        manage.run_sync({"connector": c["id"]})
        assert set(registry.access_map()) == {G._doc_name("acme/orion", "src/app.py"), G._doc_name("acme/orion", "LICENSE")}
        assert "who" in (DOCS_DIR / G._doc_name("acme/orion", "src/app.py")).read_text()
        st = manage.get(c["id"])
        assert st["last_error"] is None and st["secrets"]["token"].startswith("ghp_x"[:3])
    with core.tenant_scope(b):
        assert manage.list_all() == [] and registry.access_map() == {}


def test_webhook_checks_signature_and_queues_a_sync(ws):
    a, _, _, _ = ws
    with core.tenant_scope(a):
        c = manage.create("github", "Orion", {"repos": ["acme/orion"]}, {}, None, 60, "ann")
    body = json.dumps({"ref": "refs/heads/main"}).encode()
    assert manage.webhook("github", c["id"], body, "sha256=00", "push")[0] == 403          # no secret set yet
    with core.tenant_scope(a):
        manage.update(c["id"], secrets={"webhook_secret": "whsec-1"})
    sig = "sha256=" + hmac.new(b"whsec-1", body, hashlib.sha256).hexdigest()
    assert manage.webhook("github", c["id"], body, "sha256=bad", "push")[0] == 401
    assert manage.webhook("github", "c_nope", body, sig, "push")[0] == 404
    assert manage.webhook("github", c["id"], body, sig, "push")[0] == 202
    with core.tenant_scope(a):
        assert [j["kind"] for j in jobs.list_jobs()] == ["sync"]


def test_scheduler_queues_due_syncs(ws):
    a, _, _, _ = ws
    manage.install()
    with core.tenant_scope(a):
        c = manage.create("github", "Orion", {"repos": ["acme/orion"]}, {}, None, 60, "ann")
    assert (a, "sync", "Syncing Orion", {"connector": c["id"]}) in manage.due()
    scheduler.tick()
    with core.tenant_scope(a):
        assert any(j["kind"] == "sync" for j in jobs.list_jobs())
        manage.run_sync({"connector": c["id"]})
        assert manage.get(c["id"])["next_run"] > core.now()                  # next sync in an hour
    assert all(d[3]["connector"] != c["id"] for d in manage.due())


def test_bad_settings_are_refused(ws):
    a, _, _, _ = ws
    with core.tenant_scope(a):
        with pytest.raises(ValueError):
            manage.create("github", "x", {"repos": ["not a repo"]}, {}, None, 60, "ann")
        with pytest.raises(ValueError):
            manage.create("github", "x", {"repos": ["a/b"], "nope": 1}, {}, None, 60, "ann")
        with pytest.raises(ValueError):
            manage.create("gitlab", "x", {}, {}, None, 60, "ann")
