"""Connectors of a workspace: create, change, test, sync on a schedule or on a webhook, and apply what a sync
returns (documents, tables, the ready-made semantic layer, row access rules), then rebuild exactly what changed.

Secrets (tokens, passwords) go to the encrypted vault (app/vault.py) as connector:<id>:<field> and are never
returned by the API.
"""
import hashlib
import hmac
import json
import secrets as pysecrets
from datetime import datetime, timedelta, timezone

from app import core, vault

from .github import GitHub

KINDS = {GitHub.kind: GitHub}
try:
    from .postgres import Postgres
    KINDS[Postgres.kind] = Postgres
except ImportError:                                   # optional connector
    pass
DEFAULT_GROUPS = {"github": []}                 # GitHub: one access group per repository (repo-<owner>-<name>)


def _secret_name(cid: str, field: str) -> str:
    return f"connector:{cid}:{field}"


def _later(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def public(r) -> dict:
    cls = KINDS.get(r["kind"])
    return {"id": r["id"], "kind": r["kind"], "name": r["name"], "config": r["config"], "groups": json.loads(r["groups"]),
            "every_minutes": r["every_minutes"], "status": r["status"], "last_run": r["last_run"], "last_ok": r["last_ok"],
            "last_error": r["last_error"], "next_run": r["next_run"], "created": r["created"], "created_by": r["created_by"],
            "secrets": {f: vault.get_hint(_secret_name(r["id"], f)) for f in (cls.SECRETS if cls else {})},
            "webhook": f"/hooks/{r['kind']}/{r['id']}" if r["kind"] == "github" else None}


def catalog() -> list[dict]:
    return [{"kind": k, "label": c.label, "config": c.CONFIG, "secrets": c.SECRETS} for k, c in KINDS.items()]


def list_all() -> list[dict]:
    return [public(r) for r in core.db().execute("SELECT * FROM connectors WHERE tenant_id = ? ORDER BY created",
                                                 (core.current_tenant(),))]


def get(cid: str) -> dict | None:
    r = core.db().execute("SELECT * FROM connectors WHERE id = ? AND tenant_id = ?", (cid, core.current_tenant())).fetchone()
    return public(r) if r else None


def _row(cid: str):
    r = core.db().execute("SELECT * FROM connectors WHERE id = ? AND tenant_id = ?", (cid, core.current_tenant())).fetchone()
    if r is None:
        raise KeyError(cid)
    return r


def _instance(r):
    cls = KINDS[r["kind"]]
    sec = {f: vault.get(_secret_name(r["id"], f)) or "" for f in cls.SECRETS}
    return cls(r["id"], dict(r["config"]), sec, json.loads(r["groups"]))


def create(kind: str, name: str, config: dict, secrets: dict | None, groups: list[str] | None, every_minutes: int,
           by: str) -> dict:
    from app.engine import clean_groups
    if kind not in KINDS:
        raise ValueError(f"Unknown connector. Use one of: {', '.join(KINDS)}.")
    cls = KINDS[kind]
    unknown = set(config) - set(cls.CONFIG)
    if unknown:
        raise ValueError(f"Unknown settings for {kind}: {', '.join(sorted(unknown))}.")
    g = clean_groups(groups) if groups is not None else DEFAULT_GROUPS.get(kind, ["public"])
    cid = "c_" + pysecrets.token_hex(5)
    core.db().execute("INSERT INTO connectors (id, tenant_id, kind, name, config, groups, every_minutes, next_run, created, "
                      "created_by) VALUES (?, ?, ?, ?, ?::jsonb, ?, ?, ?, ?, ?)",
                      (cid, core.current_tenant(), kind, " ".join(name.split())[:80] or cls.label, json.dumps(config),
                       json.dumps(g), max(15, int(every_minutes)), core.now(), core.now(), by))
    for f, v in (secrets or {}).items():
        if f not in cls.SECRETS:
            raise ValueError(f"Unknown secret for {kind}: {f}.")
        vault.put(_secret_name(cid, f), v)
    cls(cid, config, {}, g)                                       # validates the config shape early
    if kind == "github":
        GitHub(cid, config, {}, g).repos()
        _import_ontology_template("github_ontology.yaml", f"connector:{cid}")
    return get(cid)


def _import_ontology_template(file: str, by: str):
    """A connector's ready-made graph ontology (approved, source=template)."""
    from pathlib import Path
    import yaml
    from graph import ontology
    path = Path(__file__).parent / "templates" / file
    if path.exists():
        ontology.import_schema(yaml.safe_load(path.read_text(encoding="utf-8")), source="template", by=by)


def update(cid: str, config: dict | None = None, secrets: dict | None = None, groups: list[str] | None = None,
           every_minutes: int | None = None, status: str | None = None) -> dict:
    from app.engine import clean_groups
    r = _row(cid)
    cls = KINDS[r["kind"]]
    if config is not None:
        if set(config) - set(cls.CONFIG):
            raise ValueError("Unknown settings.")
        core.db().execute("UPDATE connectors SET config = ?::jsonb WHERE id = ?", (json.dumps(config), cid))
    if groups is not None:
        core.db().execute("UPDATE connectors SET groups = ? WHERE id = ?", (json.dumps(clean_groups(groups)), cid))
    if every_minutes is not None:
        core.db().execute("UPDATE connectors SET every_minutes = ? WHERE id = ?", (max(15, int(every_minutes)), cid))
    if status is not None:
        if status not in ("active", "paused"):
            raise ValueError("Status is active or paused.")
        core.db().execute("UPDATE connectors SET status = ? WHERE id = ?", (status, cid))
    for f, v in (secrets or {}).items():
        if f not in cls.SECRETS:
            raise ValueError(f"Unknown secret: {f}.")
        vault.put(_secret_name(cid, f), v)
    return get(cid)


def delete(cid: str, by: str) -> dict:
    """Remove the connector, its secrets and the documents it added (tables stay until removed by hand)."""
    from rag import registry
    from rag.config import DOCS_DIR
    r = _row(cid)
    removed = []
    for name, src in registry.sources().items():
        if src["source"] == f"connector:{cid}":
            (DOCS_DIR / name).unlink(missing_ok=True)
            registry.forget(name)
            removed.append(name)
    for f in KINDS[r["kind"]].SECRETS:
        vault.put(_secret_name(cid, f), None)
    core.db().execute("DELETE FROM connectors WHERE id = ?", (cid,))
    if removed:
        from app.engine import queue_rebuild
        queue_rebuild([{"file": f"{len(removed)} removed", "kind": "document", "groups": []}], by)
    return {"removed_documents": len(removed)}


def test(cid: str) -> dict:
    return _instance(_row(cid)).test()


def queue_sync(cid: str, by: str) -> dict:
    from app import jobs
    r = _row(cid)
    return jobs.enqueue("sync", f"Syncing {r['name']}", None, by, {"connector": cid})


# ------------------------------------------------------------------ the sync job

def run_sync(info: dict):
    """Job handler: sync one connector and rebuild what changed."""
    from app import engine
    from rag import registry
    from rag.config import DOCS_DIR
    from semantic.config import DATA_DIR
    cid = info["connector"]
    r = _row(cid)
    print(f"Syncing {r['name']} ({r['kind']})")
    try:
        res = _instance(r).sync(dict(r["state"]))
    except Exception as e:                                      # noqa: BLE001 - recorded on the connector, retried later
        core.db().execute("UPDATE connectors SET last_run = ?, last_error = ?, next_run = ? WHERE id = ?",
                          (core.now(), f"{type(e).__name__}: {e}"[:600], _later(min(60, r["every_minutes"])), cid))
        raise
    changed = []
    if res.layer:
        from semantic import repo
        from semantic.schema import Enrichment, MetricSet, SemanticModel
        n = repo.import_layer(SemanticModel.model_validate(res.layer["model"]),
                              MetricSet.model_validate(res.layer["metrics"]),
                              Enrichment.model_validate(res.layer.get("enrichment") or {}),
                              source="template", by=f"connector:{cid}")
        print(f"  semantic layer: {n or 'unchanged'}")
    for d in res.documents:
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        (DOCS_DIR / d.name).write_bytes(d.data)
        registry.set_groups(d.name, d.groups, source=f"connector:{cid}", source_ref=d.ref)
        changed.append({"file": d.name, "kind": "document", "groups": d.groups})
    for name in res.deleted:
        (DOCS_DIR / name).unlink(missing_ok=True)
        registry.forget(name)
    for t in res.tables:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        old = (DATA_DIR / f"{t.name}.csv").read_bytes() if (DATA_DIR / f"{t.name}.csv").exists() else None
        if old != t.csv:
            (DATA_DIR / f"{t.name}.csv").write_bytes(t.csv)
            changed.append({"file": f"{t.name}.csv", "kind": "table"})
    if res.access_rules:
        from semantic import access
        access.replace_rules(res.access_rules, source=f"connector:{cid}")
    core.db().execute("UPDATE connectors SET state = ?::jsonb, last_run = ?, last_ok = ?, last_error = NULL, next_run = ? "
                      "WHERE id = ?", (json.dumps(res.state), core.now(), core.now(), _later(r["every_minutes"]), cid))
    print(f"  {res.summary}")
    if res.summary.get("live_tables_changed"):
        changed.append({"file": "live tables", "kind": "table"})
    if changed or res.deleted:
        docs = [c for c in changed if c["kind"] == "document"] or             ([{"kind": "document", "file": f"{len(res.deleted)} removed"}] if res.deleted else [])
        engine.rebuild({"files": docs + [c for c in changed if c["kind"] == "table"],
                        "graph": engine.graph_ready() or engine.info_graph_wanted()})
    else:
        print("  nothing changed")
    core.log_event("system", f"connector:{r['name']}", "connector.sync", r["kind"], res.summary)


# ------------------------------------------------------------------ scheduling and webhooks

def due() -> list[tuple]:
    """For the scheduler: (tenant, kind, label, info) of every active connector whose next sync is due."""
    with core.system():
        rows = core.db().execute("SELECT id, tenant_id, name FROM connectors WHERE status = 'active' "
                                 "AND (next_run IS NULL OR next_run <= ?)", (core.now(),)).fetchall()
    return [(r["tenant_id"], "sync", f"Syncing {r['name']}", {"connector": r["id"]}) for r in rows]


def webhook(kind: str, cid: str, body: bytes, signature: str, event: str) -> tuple[int, str]:
    """A source says something changed: check its signature, then queue a sync in the connector's workspace."""
    with core.system():
        r = core.db().execute("SELECT id, tenant_id, kind, name, status FROM connectors WHERE id = ?", (cid,)).fetchone()
    if r is None or r["kind"] != kind:
        return 404, "unknown connector"
    with core.tenant_scope(r["tenant_id"]):
        secret = vault.get(_secret_name(cid, "webhook_secret"))
        if not secret:
            return 403, "set a webhook secret on this connector first"
        expect = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expect, signature or ""):
            core.log_event("system", f"connector:{r['name']}", "connector.webhook", kind, {"error": "bad signature"}, ok=False)
            return 401, "bad signature"
        if event == "ping":
            return 200, "pong"
        if r["status"] != "active":
            return 202, "connector paused"
        queue_sync(cid, "webhook")
        core.log_event("system", f"connector:{r['name']}", "connector.webhook", kind, {"event": event})
    return 202, "sync queued"


def live_sources() -> list[dict]:
    """Database connectors in live mode of the current workspace, for semantic/db.py to attach read-only."""
    t = core.current_tenant()
    if not t:
        return []
    out = []
    for r in core.db().execute("SELECT * FROM connectors WHERE tenant_id = ? AND kind = 'postgres' AND status = 'active'",
                               (t,)):
        tables = (r["state"] or {}).get("live_tables") or {}
        if (r["config"] or {}).get("mode") == "live" and tables:
            dsn = vault.get(_secret_name(r["id"], "dsn"))
            if dsn:
                out.append({"alias": f"src_{r['id']}", "dsn": dsn, "tables": tables})
    return out


def install():
    """Register the sync job and the scheduler hook (called by the app at start)."""
    from app import jobs, scheduler
    jobs.register("sync", "Syncing a source", run_sync)
    if due not in scheduler.DUE:
        scheduler.DUE.append(due)
