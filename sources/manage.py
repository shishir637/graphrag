"""Sources: outside systems (GitHub, a Postgres or MySQL database, an S3-compatible folder) connected once and then
kept up to date on their own: on a schedule (app/scheduler.py) and, for GitHub, on a signed webhook.

Each source puts its files into ONE data set chosen by the admin (default: a new data set named after the source),
so who sees them is decided on the Access tab like for any uploaded file. Documents land in the documents folder
(names start with the source's prefix, e.g. gh-owner-repo--path.md) and are tracked in `source_docs`, so a sync can
replace or remove exactly the files it added. Tables land in the data folder as Parquet. A ready-made semantic
layer (GitHub's engineering metrics) is merged into the company layer once.

Secrets (tokens, connection strings, webhook secrets) are stored encrypted in app/vault.py as source:<id>:<field>
and never returned by the API (only a hint).

(The word "connectors" in this product means what Claude may use for a person, see app/access.py; outside systems
are therefore called sources everywhere.)
"""
import hashlib
import hmac
import json
import secrets as pysecrets
from datetime import datetime, timedelta, timezone

from app import core, vault

from .github import GitHub

KINDS = {GitHub.kind: GitHub}
for _mod, _cls in (("postgres", "Postgres"), ("mysql", "MySQL"), ("s3", "S3Folder")):
    try:                                              # optional (their drivers are optional dependencies)
        _c = getattr(__import__(f"sources.{_mod}", fromlist=[_cls]), _cls)
        KINDS[_c.kind] = _c
    except ImportError:
        pass
MIN_EVERY = 15

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, config TEXT NOT NULL DEFAULT '{}',
  dataset TEXT NOT NULL DEFAULT 'public', every_minutes INTEGER NOT NULL DEFAULT 360, state TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'active', last_run TEXT, last_ok TEXT, last_error TEXT, next_run TEXT,
  summary TEXT NOT NULL DEFAULT '{}', created TEXT NOT NULL, created_by TEXT NOT NULL{tenant});
CREATE TABLE IF NOT EXISTS source_docs (
  source_id TEXT NOT NULL, name TEXT NOT NULL, ref TEXT NOT NULL DEFAULT ''{tenant}, PRIMARY KEY (source_id, name));
"""
_ready: str | None = None


def db():
    return core.db_with(SCHEMA)


def _secret_name(sid: str, field: str) -> str:
    return f"source:{sid}:{field}"


def _later(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _j(s, default):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default


def public(r) -> dict:
    cls = KINDS.get(r["kind"])
    return {"id": r["id"], "kind": r["kind"], "label": cls.label if cls else r["kind"], "name": r["name"],
            "config": _j(r["config"], {}), "dataset": r["dataset"], "every_minutes": r["every_minutes"],
            "status": r["status"], "last_run": r["last_run"], "last_ok": r["last_ok"], "last_error": r["last_error"],
            "next_run": r["next_run"], "summary": _j(r["summary"], {}), "created": r["created"],
            "created_by": r["created_by"],
            "documents": db().execute("SELECT count(*) FROM source_docs WHERE source_id = ?", (r["id"],)).fetchone()[0],
            "secrets": {f: vault.get_hint(_secret_name(r["id"], f)) for f in (cls.SECRETS if cls else {})},
            "webhook": f"/hooks/{r['kind']}/{r['id']}" if getattr(cls, "WEBHOOK", False) else None}


def catalog() -> list[dict]:
    return [{"kind": k, "label": c.label, "config": c.CONFIG, "secrets": c.SECRETS, "webhook": bool(getattr(c, "WEBHOOK", False))}
            for k, c in KINDS.items()]


def list_all() -> list[dict]:
    return [public(r) for r in db().execute("SELECT * FROM sources ORDER BY created")]


def get(sid: str) -> dict | None:
    r = db().execute("SELECT * FROM sources WHERE id = ?", (sid,)).fetchone()
    return public(r) if r else None


def _row(sid: str):
    r = db().execute("SELECT * FROM sources WHERE id = ?", (sid,)).fetchone()
    if r is None:
        raise KeyError(sid)
    return r


def _instance(r):
    cls = KINDS[r["kind"]]
    sec = {f: vault.get(_secret_name(r["id"], f)) or "" for f in cls.SECRETS}
    return cls(r["id"], _j(r["config"], {}), sec, [r["dataset"]])


def _dataset(name: str | None, source_name: str, by: str) -> str:
    """The data set the source's files go into: a known one, or a new one (created here)."""
    from app import access
    from app.engine import clean_groups
    if name:
        n = clean_groups([name])[0]
        if n not in {d["name"] for d in access.datasets()}:
            access.add_dataset(n, f"Files from the source {source_name}.", by)
        return n
    base = clean_groups(["".join(ch if ch.isalnum() else "-" for ch in source_name.lower()).strip("-")[:36] or "source"])[0]
    known = {d["name"] for d in access.datasets()}
    n, i = base, 2
    while n in known:
        n, i = f"{base[:34]}-{i}", i + 1
    access.add_dataset(n, f"Files from the source {source_name}.", by)
    return n


def create(kind: str, name: str, config: dict, secrets: dict | None, dataset: str | None, every_minutes: int,
           by: str) -> dict:
    if kind not in KINDS:
        raise ValueError(f"Unknown source type. Use one of: {', '.join(KINDS)}.")
    cls = KINDS[kind]
    unknown = set(config or {}) - set(cls.CONFIG)
    if unknown:
        raise ValueError(f"Unknown settings for {kind}: {', '.join(sorted(unknown))}.")
    for f in secrets or {}:
        if f not in cls.SECRETS:
            raise ValueError(f"Unknown secret for {kind}: {f}.")
    name = " ".join((name or "").split())[:80] or cls.label
    if kind == "github":
        GitHub("check", config or {}, {}, []).repos()           # validates owner/name early
    ds = _dataset(dataset, name, by)
    sid = "s_" + pysecrets.token_hex(5)
    db().execute("INSERT INTO sources (id, kind, name, config, dataset, every_minutes, next_run, created, created_by) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (sid, kind, name, json.dumps(config or {}), ds, max(MIN_EVERY, int(every_minutes or 360)), core.now(),
                  core.now(), by))
    for f, v in (secrets or {}).items():
        vault.put(_secret_name(sid, f), v)
    return get(sid)


def update(sid: str, config: dict | None = None, secrets: dict | None = None, dataset: str | None = None,
           every_minutes: int | None = None, status: str | None = None, name: str | None = None, by: str = "") -> dict:
    r = _row(sid)
    cls = KINDS[r["kind"]]
    if config is not None:
        if set(config) - set(cls.CONFIG):
            raise ValueError("Unknown settings.")
        db().execute("UPDATE sources SET config = ? WHERE id = ?", (json.dumps(config), sid))
    if name:
        db().execute("UPDATE sources SET name = ? WHERE id = ?", (" ".join(name.split())[:80], sid))
    if dataset is not None and dataset != r["dataset"]:
        ds = _dataset(dataset, r["name"], by)
        db().execute("UPDATE sources SET dataset = ? WHERE id = ?", (ds, sid))
        from app import engine
        moved = []
        for d in db().execute("SELECT name FROM source_docs WHERE source_id = ?", (sid,)).fetchall():
            try:
                engine.set_document_groups(d["name"], [ds])
                moved.append(d["name"])
            except KeyError:
                pass
        if moved:                                          # the search index carries the data set of each passage
            engine.queue_rebuild([{"file": f"{len(moved)} files of {r['name']}", "kind": "document", "groups": [ds]}], by)
    if every_minutes is not None:
        db().execute("UPDATE sources SET every_minutes = ? WHERE id = ?", (max(MIN_EVERY, int(every_minutes)), sid))
    if status is not None:
        if status not in ("active", "paused"):
            raise ValueError("Status is active or paused.")
        db().execute("UPDATE sources SET status = ? WHERE id = ?", (status, sid))
    for f, v in (secrets or {}).items():
        if f not in cls.SECRETS:
            raise ValueError(f"Unknown secret: {f}.")
        vault.put(_secret_name(sid, f), v)
    return get(sid)


def delete(sid: str, by: str) -> dict:
    """Remove the source, its secrets and the documents it added (tables stay until removed by hand)."""
    from app import engine
    r = _row(sid)
    removed = 0
    for d in db().execute("SELECT name FROM source_docs WHERE source_id = ?", (sid,)).fetchall():
        try:
            engine.delete_document(d["name"])
            removed += 1
        except KeyError:
            pass
    for f in KINDS[r["kind"]].SECRETS if r["kind"] in KINDS else {}:
        vault.put(_secret_name(sid, f), None)
    db().execute("DELETE FROM source_docs WHERE source_id = ?", (sid,))
    db().execute("DELETE FROM sources WHERE id = ?", (sid,))
    if removed:
        engine.queue_rebuild([{"file": f"{removed} removed", "kind": "document", "groups": []}], by)
    return {"removed_documents": removed}


def test(sid: str) -> dict:
    try:
        return _instance(_row(sid)).test()
    except KeyError:
        raise
    except Exception as e:                                     # noqa: BLE001 - shown to the admin
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"[:400]}


def queue_sync(sid: str, by: str) -> dict:
    from app import jobs
    r = _row(sid)
    return jobs.enqueue(f"sync:{sid}", f"Syncing {r['name']}", run_sync, by, {"source": sid})


# ------------------------------------------------------------------ the sync job

def _import_layer(layer: dict, by: str) -> int:
    """A source's ready-made semantic layer merged into the company layer (same names: replaced). Returns the
    number of entities, metrics and synonyms added or changed."""
    from semantic import defstore, yamlio
    from semantic.config import ENRICHMENT_FILE, METRICS_FILE, MODEL_FILE
    from semantic.schema import Enrichment, MetricSet, SemanticModel
    changed = 0

    def merge(path, data, key_list, key, schema):
        nonlocal changed
        cur = yamlio._plain(yamlio.read(path)) if path.exists() or defstore.enabled() else {}
        cur = cur or {}
        for lst in key_list:
            have = {key(x): i for i, x in enumerate(cur.get(lst) or [])}
            for item in data.get(lst) or []:
                k = key(item)
                if k in have:
                    if cur[lst][have[k]] != item:
                        cur[lst][have[k]] = item
                        changed += 1
                else:
                    cur.setdefault(lst, []).append(item)
                    changed += 1
        for k, v in data.items():
            if k not in key_list:
                cur.setdefault(k, v)
        schema.model_validate(cur)
        yamlio.write(path, cur)

    ctx = defstore.acting(by, "source") if defstore.enabled() else None
    if ctx:
        ctx.__enter__()
    try:
        merge(MODEL_FILE, layer["model"], ["entities", "joins"],
              lambda d: d.get("name") or f"{d.get('from_entity')}.{d.get('from_column')}>{d.get('to_entity')}", SemanticModel)
        merge(METRICS_FILE, layer["metrics"], ["metrics"], lambda d: d["name"], MetricSet)
        if layer.get("enrichment"):
            merge(ENRICHMENT_FILE, layer["enrichment"], ["synonyms"],
                  lambda d: f"{str(d.get('term', '')).lower()}>{d.get('target')}", Enrichment)
    finally:
        if ctx:
            ctx.__exit__(None, None, None)
    return changed


def run_sync(info: dict):
    """Job handler: sync one source and rebuild what changed."""
    from app import engine
    from .files import store_table
    sid = info["source"]
    r = _row(sid)
    by = f"source:{r['name']}"
    print(f"Syncing {r['name']} ({r['kind']})")
    try:
        res = _instance(r).sync(_j(r["state"], {}))
    except Exception as e:                                      # noqa: BLE001 - recorded on the source, retried later
        db().execute("UPDATE sources SET last_run = ?, last_error = ?, next_run = ? WHERE id = ?",
                     (core.now(), f"{type(e).__name__}: {e}"[:600], _later(min(60, r["every_minutes"])), sid))
        core.log_event("system", by, "source.sync", r["kind"], {"error": f"{type(e).__name__}: {e}"[:300]}, ok=False)
        raise
    changed = []
    ds = r["dataset"]
    if res.layer:
        n = _import_layer(res.layer, by)
        print(f"  semantic layer: {n or 'unchanged'}")
    for d in res.documents:
        saved = engine.save_document(d.name, d.data, [ds])
        db().execute("INSERT INTO source_docs (source_id, name, ref) VALUES (?, ?, ?) ON CONFLICT (source_id, name) "
                     "DO UPDATE SET ref = excluded.ref", (sid, saved["name"], d.ref))
        changed.append({"file": saved["name"], "kind": "document", "groups": [ds]})
    removed = 0
    for name in res.deleted:
        try:
            engine.delete_document(name)
            removed += 1
        except (KeyError, ValueError):
            pass
        db().execute("DELETE FROM source_docs WHERE source_id = ? AND name = ?", (sid, name))
    for t in res.tables:
        if store_table(t.name, parquet_path=t.parquet_path, csv=t.csv if not t.parquet_path else None,
                       source=f"source:{sid}"):
            changed.append({"file": f"{t.name}.parquet", "kind": "table"})
    if res.summary.get("live_tables_changed"):
        changed.append({"file": "live tables", "kind": "table"})
    summary = dict(res.summary) | {"documents_added_or_changed": len(res.documents), "documents_removed": removed}
    db().execute("UPDATE sources SET state = ?, last_run = ?, last_ok = ?, last_error = NULL, next_run = ?, summary = ? "
                 "WHERE id = ?", (json.dumps(res.state), core.now(), core.now(), _later(r["every_minutes"]),
                                  json.dumps(summary, default=str), sid))
    print(f"  {summary}")
    if changed or removed:
        docs = [c for c in changed if c["kind"] == "document"] or \
            ([{"kind": "document", "file": f"{removed} removed", "groups": []}] if removed else [])
        engine.rebuild({"files": docs + [c for c in changed if c["kind"] == "table"],
                        "graph": engine.graph_ready() or engine.info_graph_wanted()})
    else:
        print("  nothing changed")
    core.log_event("system", by, "source.sync", r["kind"], summary)


# ------------------------------------------------------------------ scheduling and webhooks

def due() -> list[tuple[str, str]]:
    """For the scheduler: (source id, workspace) of every active source whose next sync is due, in every workspace."""
    from app import db as D
    with core.system():
        rows = db().execute("SELECT * FROM sources WHERE status = 'active' AND (next_run IS NULL OR next_run <= ?)",
                            (core.now(),)).fetchall()
    return [(r["id"], core._tenant(r) if D.postgres() else None) for r in rows]


def run_due() -> int:
    """Queue a sync for every source that is due (its next_run moves on, so it is queued once)."""
    n = 0
    for sid, tid in due():
        with core.tenant_scope(tid):
            r = _row(sid)
            db().execute("UPDATE sources SET next_run = ? WHERE id = ?", (_later(r["every_minutes"]), sid))
            queue_sync(sid, "schedule")
        n += 1
    return n


def webhook(kind: str, sid: str, body: bytes, signature: str, event: str) -> tuple[int, str]:
    """A source says something changed: check its HMAC-SHA256 signature, then queue a sync."""
    r = db().execute("SELECT id, kind, name, status FROM sources WHERE id = ?", (sid,)).fetchone()
    if r is None or r["kind"] != kind or not getattr(KINDS.get(kind), "WEBHOOK", False):
        return 404, "unknown source"
    secret = vault.get(_secret_name(sid, "webhook_secret"))
    if not secret:
        return 403, "set a webhook secret on this source first"
    expect = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, signature or ""):
        core.log_event("system", f"source:{r['name']}", "source.webhook", kind, {"error": "bad signature"}, ok=False)
        return 401, "bad signature"
    if event == "ping":
        return 200, "pong"
    if r["status"] != "active":
        return 202, "source paused"
    queue_sync(sid, "webhook")
    core.log_event("system", f"source:{r['name']}", "source.webhook", kind, {"event": event})
    return 202, "sync queued"


def live_sources() -> list[dict]:
    """SQL sources in live mode, for semantic/db.py to attach read-only:
    [{"alias", "type" (postgres | mysql), "attach" (DuckDB ATTACH string), "tables": {name: "schema.table"}}]."""
    from .sql_base import SqlSource
    out = []
    for r in db().execute("SELECT * FROM sources WHERE status = 'active'").fetchall():
        cls = KINDS.get(r["kind"])
        if not cls or not issubclass(cls, SqlSource):
            continue
        tables = _j(r["state"], {}).get("live_tables") or {}
        if _j(r["config"], {}).get("mode") == "live" and tables:
            inst = _instance(r)
            if inst.secrets.get("dsn"):
                out.append({"alias": f"src_{r['id']}", "type": inst.DUCKDB_TYPE, "attach": inst.duckdb_attach(),
                            "tables": tables})
    return out
