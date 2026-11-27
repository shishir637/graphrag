"""Definitions store: the semantic layer of every layer (company + each person's) in Postgres, with history.

The semantic code keeps reading and writing "files" through semantic/yamlio.py. When the store is on, those
calls land here instead:
  read(model.yaml)   -> the definitions of the caller's layer stack, merged (company first, then shared
                        layers, then their own), in the same shape as the YAML file
  write(model.yaml)  -> the differences are saved into ONE layer (the company layer unless a tool says
                        otherwise), one history row per changed item: who, when, from which app, before/after
The company layer is also written back to semantic_layer/*.yaml as a readable export, so Git review and the
existing YAML tools keep working. Postgres is the source of truth.

On: CL_DEFS_DSN (a Postgres DSN, or sqlite:///path for tests). Docker points it at the graph database, in a
schema of its own (cl_defs). Off (plain YAML files, as before): no CL_DEFS_DSN, or CL_DEFS=yaml.
"""
import contextvars
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

COMPANY = "company"

# what each layer file is made of: (top-level list, kind, key of one item)
SPEC: dict[str, list[tuple[str, str, object]]] = {
    "model.yaml": [("entities", "entity", lambda d: d["name"]),
                   ("joins", "join", lambda d: f"{d['from_entity']}.{d['from_column']}>{d['to_entity']}.{d['to_column']}")],
    "metrics.yaml": [("metrics", "metric", lambda d: d["name"])],
    "enrichment.yaml": [("synonyms", "synonym", lambda d: f"{str(d['term']).lower()}>{d['target']}"),
                        ("value_list_dimensions", "value_list", lambda s: str(s))],
    "verified_queries.yaml": [("queries", "verified", lambda d: d["question"])],
    "ontology.yaml": [("entity_types", "entity_type", lambda d: d["name"]),
                      ("relation_types", "relation_type", lambda d: d["name"])],
}
KINDS = {kind for parts in SPEC.values() for _, kind, _ in parts} | {"rule", "meta"}

STACK: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar("cl_layer_stack", default=(COMPANY,))
WRITE_LAYER: contextvars.ContextVar[str] = contextvars.ContextVar("cl_write_layer", default=COMPANY)
ACTOR: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar("cl_actor", default=("system", "app"))

_lock = threading.RLock()
_local = threading.local()
_ready: str | None = None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dsn() -> str | None:
    if os.environ.get("CL_DEFS", "").lower() == "yaml":
        return None
    return os.environ.get("CL_DEFS_DSN") or None


def enabled() -> bool:
    return bool(dsn())


# ------------------------------------------------------------------ connection (Postgres, or SQLite for tests)

def _schema() -> str:
    """Each workspace keeps its definitions in a schema of its own: cl_defs (the first workspace) or
    cl_defs_<workspace id>. With a SQLite store (tests), a file per workspace next to the configured one."""
    from rag.wspath import workspace_key
    k = workspace_key()
    return f"cl_defs_{k}"[:60] if k else "cl_defs"


class _DB:
    def __init__(self, d: str):
        self.sqlite = d.startswith("sqlite:///")
        if self.sqlite:
            path = d.removeprefix("sqlite:///")
            if _schema() != "cl_defs":
                path = f"{path}.{_schema()}"
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.c = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self.c.row_factory = sqlite3.Row
        else:
            import psycopg
            from psycopg.rows import dict_row
            self.c = psycopg.connect(d, autocommit=True, row_factory=dict_row)
            self.c.execute(f"CREATE SCHEMA IF NOT EXISTS {_schema()}")
            self.c.execute(f"SET search_path TO {_schema()}")

    def q(self, sql: str, args=()):
        if not self.sqlite:
            sql = sql.replace("?", "%s")
        return self.c.execute(sql, args)

    def all(self, sql: str, args=()) -> list[dict]:
        return [dict(r) for r in self.q(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        r = self.q(sql, args).fetchone()
        return dict(r) if r else None

    @contextmanager
    def tx(self):
        if self.sqlite:
            self.c.execute("BEGIN IMMEDIATE")
            try:
                yield self
                self.c.execute("COMMIT")
            except BaseException:
                self.c.execute("ROLLBACK")
                raise
        else:
            with self.c.transaction():
                yield self


def db() -> _DB:
    d = dsn()
    if not d:
        raise RuntimeError("the definitions store is off (CL_DEFS=yaml)")
    key = f"{d}|{_schema()}"
    cache = _local.__dict__.setdefault("dbs", {})
    cur = cache.get(key)
    if cur is None:
        cur = cache[key] = _DB(d)
    _ensure(cur, key)
    return cur


_ready_keys: set = set()


def _ensure(x: _DB, d: str):
    if d in _ready_keys:
        return
    with _lock:
        if d in _ready_keys:
            return
        serial = "INTEGER PRIMARY KEY AUTOINCREMENT" if x.sqlite else "BIGSERIAL PRIMARY KEY"
        for sql in (
            "CREATE TABLE IF NOT EXISTS layers (id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT, name TEXT NOT NULL,"
            " created TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS definitions (layer_id TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL,"
            " pos INTEGER NOT NULL DEFAULT 0, body TEXT, removed INTEGER NOT NULL DEFAULT 0, created_by TEXT,"
            " updated_by TEXT, updated TEXT, PRIMARY KEY (layer_id, kind, key))",
            f"CREATE TABLE IF NOT EXISTS history (id {serial}, layer_id TEXT NOT NULL, kind TEXT NOT NULL,"
            " key TEXT NOT NULL, action TEXT NOT NULL, before TEXT, after TEXT, by_user TEXT, app TEXT, at TEXT NOT NULL,"
            " note TEXT, undone_by INTEGER)",
            "CREATE INDEX IF NOT EXISTS history_item ON history (layer_id, kind, key)",
        ):
            x.q(sql)
        if not x.one("SELECT id FROM layers WHERE id = ?", (COMPANY,)):
            x.q("INSERT INTO layers VALUES (?, 'company', NULL, 'Company', ?)", (COMPANY, now()))
        _ready_keys.add(d)
    _migrate_once()


def reset_for_tests():
    global _ready
    _ready = None
    _ready_keys.clear()
    _migrated.clear()
    _local.__dict__.clear()


# ------------------------------------------------------------------ layers

def ensure_layer(layer_id: str, owner: str | None, name: str, kind: str = "personal") -> str:
    x = db()
    if not x.one("SELECT id FROM layers WHERE id = ?", (layer_id,)):
        x.q("INSERT INTO layers VALUES (?, ?, ?, ?, ?)", (layer_id, kind, owner, name, now()))
    return layer_id


def layer(layer_id: str) -> dict | None:
    return db().one("SELECT * FROM layers WHERE id = ?", (layer_id,))


# ------------------------------------------------------------------ merged view of a stack

def _rows(stack: tuple[str, ...], kinds: set[str]) -> dict[tuple[str, str], dict]:
    """(kind, key) -> the winning row: later layers in the stack override earlier ones."""
    x = db()
    marks = ",".join("?" * len(stack))
    rows = x.all(f"SELECT * FROM definitions WHERE layer_id IN ({marks})", tuple(stack))
    order = {lid: i for i, lid in enumerate(stack)}
    win: dict[tuple[str, str], dict] = {}
    for r in sorted(rows, key=lambda r: (order[r["layer_id"]], r["pos"])):
        if r["kind"] in kinds:
            first = win.get((r["kind"], r["key"]))
            r["_pos"] = first["_pos"] if first else (order[r["layer_id"]], r["pos"])
            win[(r["kind"], r["key"])] = r
    return win


def compose(filename: str, stack: tuple[str, ...] | None = None) -> dict:
    stack = stack or STACK.get()
    spec = SPEC[filename]
    rows = _rows(stack, {k for _, k, _ in spec} | {"meta"})
    out: dict = {}
    for (kind, key), r in rows.items():
        if kind == "meta" and key.startswith(filename + ":") and not r["removed"]:
            out[key.split(":", 1)[1]] = json.loads(r["body"])
    out.setdefault("version", 1)
    for top, kind, _ in spec:
        items = sorted((r for (k, _), r in rows.items() if k == kind and not r["removed"]), key=lambda r: r["_pos"])
        out[top] = [json.loads(r["body"]) for r in items]
    return out


def source_of(kind: str, key: str, stack: tuple[str, ...] | None = None) -> dict | None:
    """Which layer the caller's definition of one item comes from, and who last changed it."""
    stack = stack or STACK.get()
    r = _rows(stack, {kind}).get((kind, key))
    if not r or r["removed"]:
        return None
    h = db().one("SELECT by_user, at FROM history WHERE layer_id = ? AND kind = ? AND key = ? ORDER BY id DESC LIMIT 1",
                 (r["layer_id"], kind, key))
    return {"layer": r["layer_id"], "by": (h or {}).get("by_user") or r["updated_by"], "at": (h or {}).get("at") or r["updated"],
            "body": json.loads(r["body"])}


def version(stack: tuple[str, ...] | None = None) -> str:
    stack = stack or STACK.get()
    marks = ",".join("?" * len(stack))
    r = db().one(f"SELECT MAX(id) AS v, COUNT(*) AS n FROM history WHERE layer_id IN ({marks})", tuple(stack))
    return f"{'+'.join(stack)}:{(r or {}).get('v') or 0}:{(r or {}).get('n') or 0}"


# ------------------------------------------------------------------ changes (every one recorded)

def _record(x: _DB, layer_id, kind, key, action, before, after, note=""):
    by, app = ACTOR.get()
    x.q("INSERT INTO history (layer_id, kind, key, action, before, after, by_user, app, at, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (layer_id, kind, key, action, before, after, by, app, now(), note))


def put(layer_id: str, kind: str, key: str, body, note: str = "", pos: int | None = None, x: _DB | None = None) -> str:
    """Add or change one item in one layer. Returns 'added', 'changed' or 'same'."""
    x = x or db()
    new = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
    cur = x.one("SELECT body, removed, pos FROM definitions WHERE layer_id = ? AND kind = ? AND key = ?",
                (layer_id, kind, key))
    by, _ = ACTOR.get()
    if cur and not cur["removed"] and cur["body"] == new:
        if pos is not None and pos != cur["pos"]:
            x.q("UPDATE definitions SET pos = ? WHERE layer_id = ? AND kind = ? AND key = ?", (pos, layer_id, kind, key))
        return "same"
    if pos is None:
        m = x.one("SELECT MAX(pos) AS p FROM definitions WHERE layer_id = ? AND kind = ?", (layer_id, kind))
        pos = cur["pos"] if cur else ((m or {}).get("p") or 0) + 1
    if cur:
        x.q("UPDATE definitions SET body = ?, removed = 0, pos = ?, updated_by = ?, updated = ? "
            "WHERE layer_id = ? AND kind = ? AND key = ?", (new, pos, by, now(), layer_id, kind, key))
    else:
        x.q("INSERT INTO definitions VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)", (layer_id, kind, key, pos, new, by, by, now()))
    before = cur["body"] if cur and not cur["removed"] else None
    _record(x, layer_id, kind, key, "changed" if before else "added", before, new, note)
    return "changed" if before else "added"


def remove(layer_id: str, kind: str, key: str, note: str = "", tombstone: bool = False, x: _DB | None = None) -> bool:
    """Remove an item from a layer. tombstone=True hides a lower layer's item for this layer's readers."""
    x = x or db()
    cur = x.one("SELECT body, removed FROM definitions WHERE layer_id = ? AND kind = ? AND key = ?", (layer_id, kind, key))
    by, _ = ACTOR.get()
    if tombstone:
        if cur:
            x.q("UPDATE definitions SET removed = 1, updated_by = ?, updated = ? WHERE layer_id = ? AND kind = ? AND key = ?",
                (by, now(), layer_id, kind, key))
        else:
            x.q("INSERT INTO definitions VALUES (?, ?, ?, 0, NULL, 1, ?, ?, ?)", (layer_id, kind, key, by, by, now()))
    elif cur:
        x.q("DELETE FROM definitions WHERE layer_id = ? AND kind = ? AND key = ?", (layer_id, kind, key))
    else:
        return False
    _record(x, layer_id, kind, key, "removed", cur["body"] if cur and not cur["removed"] else None, None, note)
    return True


def save_file(filename: str, data: dict, layer_id: str | None = None, note: str = "") -> dict:
    """A whole file's content becomes one layer's content: only the differences are written and recorded."""
    layer_id = layer_id or WRITE_LAYER.get()
    if layer_id != COMPANY:
        return _save_overlay(filename, data, layer_id, note)
    x = db()
    counts = {"added": 0, "changed": 0, "removed": 0}
    with _lock, x.tx():
        for k, v in data.items():
            if k not in {top for top, _, _ in SPEC[filename]}:
                if put(layer_id, "meta", f"{filename}:{k}", v, note, x=x) != "same":
                    counts["changed"] += 1
        for top, kind, keyf in SPEC[filename]:
            want = {}
            for i, item in enumerate(data.get(top) or []):
                want[keyf(item)] = (i, item)
            have = {r["key"] for r in x.all("SELECT key FROM definitions WHERE layer_id = ? AND kind = ? AND removed = 0",
                                             (layer_id, kind))}
            for key, (i, item) in want.items():
                r = put(layer_id, kind, key, item, note, pos=i, x=x)
                if r != "same":
                    counts[r] += 1
            for key in have - set(want):
                remove(layer_id, kind, key, note, x=x)
                counts["removed"] += 1
    if layer_id == COMPANY:
        export_company()
    return counts


def _save_overlay(filename: str, data: dict, layer_id: str, note: str) -> dict:
    """A personal layer keeps only its differences from the layers under it: items equal to the base are not
    copied, items it drops from the base are hidden for its readers (tombstones)."""
    base_stack = tuple(l for l in STACK.get() if l != layer_id) or (COMPANY,)
    x = db()
    counts = {"added": 0, "changed": 0, "removed": 0}
    with _lock, x.tx():
        for top, kind, keyf in SPEC[filename]:
            base = {keyf(i): json.dumps(i, ensure_ascii=False, sort_keys=True, default=str)
                    for i in compose(filename, base_stack).get(top) or []}
            want = {keyf(i): i for i in data.get(top) or []}
            mine = {r["key"]: r for r in x.all("SELECT key, removed FROM definitions WHERE layer_id = ? AND kind = ?",
                                                (layer_id, kind))}
            for key, item in want.items():
                same = base.get(key) == json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                if same:
                    if key in mine:
                        remove(layer_id, kind, key, note, x=x)
                        counts["removed"] += 1
                    continue
                r = put(layer_id, kind, key, item, note, x=x)
                if r != "same":
                    counts[r] += 1
            for key in set(base) - set(want):
                if not (key in mine and mine[key]["removed"]):
                    remove(layer_id, kind, key, note, tombstone=True, x=x)
                    counts["removed"] += 1
            for key in set(mine) - set(want) - set(base):
                remove(layer_id, kind, key, note, x=x)
                counts["removed"] += 1
    return counts


# ------------------------------------------------------------------ changes waiting for the person's yes

def _pending_table(x: _DB):
    x.q("CREATE TABLE IF NOT EXISTS pending (code TEXT PRIMARY KEY, layer_id TEXT NOT NULL, kind TEXT NOT NULL,"
        " key TEXT NOT NULL, body TEXT, by_user TEXT NOT NULL, created TEXT NOT NULL, summary TEXT)")


def hold(layer_id: str, kind: str, key: str, body, by: str, summary: str) -> str:
    """Prepare a change; nothing is saved until confirm() comes back with this code from the same person."""
    import secrets
    x = db()
    _pending_table(x)
    code = secrets.token_hex(4)
    x.q("INSERT INTO pending VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (code, layer_id, kind, key, None if body is None else json.dumps(body, default=str), by, now(), summary))
    return code


def confirm(code: str, by: str, max_age_s: int = 3600) -> dict:
    x = db()
    _pending_table(x)
    p = x.one("SELECT * FROM pending WHERE code = ?", ((code or "").strip(),))
    if p is None or p["by_user"] != by:
        raise KeyError("No change is waiting with that code. Prepare the change again.")
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(p["created"])).total_seconds()
    x.q("DELETE FROM pending WHERE code = ?", (p["code"],))
    if age > max_age_s:
        raise KeyError("That change waited too long. Prepare it again.")
    if p["layer_id"].startswith("request:"):           # needs an admin's approval: queue it, change nothing yet
        target = p["layer_id"][8:]
        rid = request(target, p["kind"], p["key"], None if p["body"] is None else json.loads(p["body"]), by,
                      p["summary"] or "")
        return {"saved": False, "requested": True, "request_id": rid, "layer": target,
                "item": f"{p['kind']} {p['key']}", "summary": p["summary"]}
    with _lock, x.tx():
        if p["body"] is None:
            remove(p["layer_id"], p["kind"], p["key"], p["summary"] or "", tombstone=p["layer_id"] != COMPANY, x=x)
        else:
            put(p["layer_id"], p["kind"], p["key"], json.loads(p["body"]), p["summary"] or "", x=x)
    if p["layer_id"] == COMPANY:
        export_company()
    return {"saved": True, "layer": p["layer_id"], "item": f"{p['kind']} {p['key']}", "summary": p["summary"]}


# ------------------------------------------------------------------ changes waiting for an admin's approval

def _approvals_table(x: _DB):
    serial = "INTEGER PRIMARY KEY AUTOINCREMENT" if x.sqlite else "BIGSERIAL PRIMARY KEY"
    x.q(f"CREATE TABLE IF NOT EXISTS approvals (id {serial}, layer_id TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL,"
        " body TEXT, by_user TEXT NOT NULL, app TEXT, created TEXT NOT NULL, summary TEXT, status TEXT NOT NULL,"
        " decided_by TEXT, decided TEXT, reason TEXT)")


def request(layer_id: str, kind: str, key: str, body, by: str, summary: str) -> int:
    """Ask for a change to a shared layer (a team's or the company's). Nothing changes until it is approved."""
    x = db()
    _approvals_table(x)
    _, app = ACTOR.get()
    x.q("INSERT INTO approvals (layer_id, kind, key, body, by_user, app, created, summary, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'waiting')",
        (layer_id, kind, key, None if body is None else json.dumps(body, default=str), by, app, now(), summary))
    r = x.one("SELECT MAX(id) AS id FROM approvals WHERE by_user = ? AND key = ?", (by, key))
    return int(r["id"])


def requests(layer_ids: list[str] | None = None, status: str | None = "waiting", by: str | None = None,
             limit: int = 200) -> list[dict]:
    x = db()
    _approvals_table(x)
    sql, args = "SELECT * FROM approvals WHERE 1 = 1", []
    if layer_ids is not None:
        if not layer_ids:
            return []
        sql += f" AND layer_id IN ({','.join('?' * len(layer_ids))})"
        args += layer_ids
    if status:
        sql += " AND status = ?"
        args.append(status)
    if by:
        sql += " AND by_user = ?"
        args.append(by)
    rows = x.all(sql + f" ORDER BY id DESC LIMIT {int(limit)}", tuple(args))
    return [{**r, "body": json.loads(r["body"]) if r["body"] else None} for r in rows]


def decide(rid: int, approve: bool, by: str, reason: str = "") -> dict:
    """Approve (the change is made in that layer, recorded as asked by the requester and approved by `by`) or
    decline a waiting request."""
    x = db()
    _approvals_table(x)
    r = x.one("SELECT * FROM approvals WHERE id = ?", (int(rid),))
    if r is None or r["status"] != "waiting":
        raise KeyError("That request is not waiting any more.")
    x.q("UPDATE approvals SET status = ?, decided_by = ?, decided = ?, reason = ? WHERE id = ?",
        ("approved" if approve else "declined", by, now(), reason, int(rid)))
    if approve:
        note = f"{r['summary'] or ''} (asked by {r['by_user']}, approved by {by})".strip()
        with acting(by, "admin console", write_layer=r["layer_id"]), _lock, x.tx():
            if r["body"] is None:
                remove(r["layer_id"], r["kind"], r["key"], note, tombstone=r["layer_id"] != COMPANY, x=x)
            else:
                put(r["layer_id"], r["kind"], r["key"], json.loads(r["body"]), note, x=x)
        if r["layer_id"] == COMPANY:
            export_company()
    return {"id": int(rid), "status": "approved" if approve else "declined", "layer": r["layer_id"],
            "item": f"{r['kind']} {r['key']}", "asked_by": r["by_user"]}


def items(layer_id: str) -> list[dict]:
    rows = db().all("SELECT kind, key, body, removed, updated_by, updated FROM definitions WHERE layer_id = ? "
                    "ORDER BY kind, pos", (layer_id,))
    return [{"kind": r["kind"], "key": r["key"], "removed": bool(r["removed"]), "updated": r["updated"],
             "by": r["updated_by"], "body": json.loads(r["body"]) if r["body"] else None} for r in rows]


def history(layer_ids: list[str] | None = None, kind: str | None = None, key: str | None = None,
            limit: int = 100) -> list[dict]:
    sql, args = "SELECT * FROM history WHERE 1 = 1", []
    if layer_ids:
        sql += f" AND layer_id IN ({','.join('?' * len(layer_ids))})"
        args += layer_ids
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    if key:
        sql += " AND key = ?"
        args.append(key)
    rows = db().all(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
    for r in rows:
        for f in ("before", "after"):
            r[f] = json.loads(r[f]) if r[f] else None
    return rows


def undo(history_id: int) -> dict:
    """Put an item back the way it was before one change (recorded as a change of its own)."""
    x = db()
    h = x.one("SELECT * FROM history WHERE id = ?", (history_id,))
    if h is None:
        raise KeyError("no such change")
    if h["undone_by"]:
        raise ValueError("this change was already undone")
    later = x.one("SELECT id FROM history WHERE layer_id = ? AND kind = ? AND key = ? AND id > ? AND action != 'undo-mark'",
                  (h["layer_id"], h["kind"], h["key"], history_id))
    if later:
        raise ValueError("this item was changed again afterwards; undo the later change first")
    with _lock, x.tx():
        if h["before"] is None:
            remove(h["layer_id"], h["kind"], h["key"], note=f"undo of change {history_id}", x=x)
        else:
            put(h["layer_id"], h["kind"], h["key"], json.loads(h["before"]), note=f"undo of change {history_id}", x=x)
        last = x.one("SELECT MAX(id) AS id FROM history")
        x.q("UPDATE history SET undone_by = ? WHERE id = ?", (last["id"], history_id))
    if h["layer_id"] == COMPANY:
        export_company()
    return {"undone": history_id, "item": f"{h['kind']} {h['key']}", "layer": h["layer_id"]}


# ------------------------------------------------------------------ YAML import / export

def import_dir(folder: Path, layer_id: str = COMPANY, note: str = "imported from YAML") -> dict:
    from .yamlio import _plain, raw_read
    total = {}
    for name in SPEC:
        f = folder / name
        if f.exists():
            data = _plain(raw_read(f)) or {}
            total[name] = save_file(name, data, layer_id, note) if layer_id != COMPANY else _save_no_export(name, data, note)
    if layer_id == COMPANY:
        export_company()
    return total


def _save_no_export(name, data, note):
    x = db()
    counts = {"added": 0, "changed": 0, "removed": 0}
    with _lock, x.tx():
        for k, v in data.items():
            if k not in {top for top, _, _ in SPEC[name]}:
                put(COMPANY, "meta", f"{name}:{k}", v, note, x=x)
        for top, kind, keyf in SPEC[name]:
            for i, item in enumerate(data.get(top) or []):
                r = put(COMPANY, kind, keyf(item), item, note, pos=i, x=x)
                if r != "same":
                    counts[r] += 1
    return counts


def export(folder: Path, stack: tuple[str, ...] | None = None):
    from .yamlio import raw_write
    folder.mkdir(parents=True, exist_ok=True)
    for name in SPEC:
        raw_write(folder / name, compose(name, stack or (COMPANY,)),
                  "Exported from the Context Layer definitions store. Postgres is the source of truth.")


def export_company():
    """Keep semantic_layer/*.yaml as a readable copy of the company layer (off with CL_DEFS_EXPORT=0)."""
    if os.environ.get("CL_DEFS_EXPORT", "1") == "0":
        return
    from .config import LAYER_DIR
    export(LAYER_DIR, (COMPANY,))


_migrated: set = set()                   # workspaces (schemas) already brought in line with their YAML


def _migrate_once():
    """First start with the store on: today's YAML becomes the company layer. Later starts: the YAML files are
    rewritten from the store, so a restored database always wins over stale files. Once per workspace."""
    if _schema() in _migrated:
        return
    _migrated.add(_schema())
    from .config import LAYER_DIR
    x = db()
    has = x.one("SELECT COUNT(*) AS n FROM definitions WHERE layer_id = ?", (COMPANY,))["n"]
    token = ACTOR.set(("migration", "app"))
    try:
        if not has and any((LAYER_DIR / n).exists() for n in SPEC):
            import_dir(LAYER_DIR)
        elif has:
            export_company()
    finally:
        ACTOR.reset(token)


def reset_migration_flag():
    _migrated.clear()


@contextmanager
def acting(by: str, app: str = "app", stack: tuple[str, ...] | None = None, write_layer: str | None = None):
    """Run code as someone: their layer stack for reads, their layer for writes, their name on the history."""
    t1 = ACTOR.set((by, app))
    t2 = STACK.set(stack) if stack else None
    t3 = WRITE_LAYER.set(write_layer) if write_layer else None
    try:
        yield
    finally:
        ACTOR.reset(t1)
        if t2:
            STACK.reset(t2)
        if t3:
            WRITE_LAYER.reset(t3)
