"""Move an existing single-workspace installation (SQLite var/app.db, before 0.5) into Postgres as one workspace.

    python -m app.migrate_local [path/to/app.db] [--name "Acme"]

People keep their passwords, roles and groups; agent keys keep working; the activity log and settings come along.
Sessions and sign-ins from Claude/ChatGPT are not copied (people sign in again). Run it once, on an empty database.
"""
import argparse
import json
import sqlite3
import sys

from . import core
from .db import system, tenant_scope, tx


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("path", nargs="?", default=str(core.DATA_DIR / "app.db"))
    ap.add_argument("--name", default=None, help="workspace name (default: the organisation setting)")
    a = ap.parse_args(argv)
    src = sqlite3.connect(a.path)
    src.row_factory = sqlite3.Row
    if core.user_count():
        sys.exit("The database already has people in it; migrate into an empty database.")
    kv = {r["key"]: json.loads(r["value"]) for r in src.execute("SELECT key, value FROM kv")}
    tid = core.create_tenant(a.name or kv.get("organisation") or "Workspace")
    cols = {r[1] for r in src.execute("PRAGMA table_info(users)")}
    with tenant_scope(tid), tx() as c:
        for u in src.execute("SELECT * FROM users ORDER BY id"):
            uid = c.execute("INSERT INTO users (email, name, pw_hash, created, last_login, disabled) "
                            "VALUES (?, ?, ?, ?, ?, 0) RETURNING id",
                            (u["email"], u["name"], u["pw_hash"], u["created"], u["last_login"])).fetchone()[0]
            c.execute("INSERT INTO memberships (user_id, tenant_id, role, groups, disabled, created) VALUES (?, ?, ?, ?, ?, ?)",
                      (uid, tid, u["role"], u["groups"] if "groups" in cols else '["public"]', u["disabled"], u["created"]))
        for g in src.execute("SELECT * FROM agents"):
            c.execute("INSERT INTO agents (id, tenant_id, name, description, groups, capabilities, scope, token_hash, prefix, "
                      "created, created_by, last_used, calls, revoked) VALUES (?, ?, ?, ?, ?, ?, 'agent', ?, ?, ?, ?, ?, ?, ?)",
                      (g["id"], tid, g["name"], g["description"], g["groups"], g["capabilities"], g["token_hash"],
                       g["prefix"], g["created"], g["created_by"], g["last_used"], g["calls"], g["revoked"]))
        n = 0
        for e in src.execute("SELECT * FROM events ORDER BY id"):
            c.execute("INSERT INTO events (tenant_id, time, actor_type, actor, action, target, detail, ok, seconds) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (tid, e["time"], e["actor_type"], e["actor"], e["action"], e["target"], e["detail"], e["ok"], e["seconds"]))
            n += 1
        for k, v in kv.items():
            if k == "oauth_hmac":
                continue
            c.execute("INSERT INTO settings (tenant_id, key, value) VALUES (?, ?, ?)", (tid, k, json.dumps(v)))
    if "oauth_hmac" in kv:
        with system():
            core.set_system_setting("oauth_hmac", kv["oauth_hmac"])
    from semantic import repo
    from semantic.config import LAYER_DIR
    layer = {}
    if (LAYER_DIR / "model.yaml").exists():
        with tenant_scope(tid):
            layer = repo.import_dir(LAYER_DIR, source="import", by="migrate_local", certified=True)
    print(f"Moved into workspace {tid}: {core.user_count()} people, events {n}, semantic layer {layer}.")


if __name__ == "__main__":
    main()
