"""Copy an existing var/app.db (SQLite, 0.8 and earlier) into Postgres, once and idempotently.

Runs on the first database use when the app is on Postgres and DATA_DIR/app.db exists: every table the two have
in common is copied (shared columns only, rows already in Postgres kept: ON CONFLICT DO NOTHING), id sequences are
moved past the copied ids, and the settings key _migrated_from_sqlite records what was copied. The SQLite file is
left untouched (rename it once you are happy). Run by hand: python -m app.migrate_local
"""
import json
import sqlite3
import threading

from . import core

FIRST = ["users", "chats"]                  # referenced by other tables
MARKER = "_migrated_from_sqlite"
_lock = threading.Lock()
_done: set = set()


def _ensure_all_tables():
    from . import access, chat, directory, doc_requests, quality, teams, vault
    access.db(); teams.db(); directory.db(); chat.db(); doc_requests.db(); quality.db(); vault._db()
    try:
        from sources import manage
        manage.db()
    except ImportError:
        pass


def migrate(path=None, force: bool = False) -> dict:
    """The old single-company install becomes the first workspace ("default")."""
    from . import db as D
    with D.tenant_scope(D.DEFAULT):
        return _migrate(path, force)


def _migrate(path=None, force: bool = False) -> dict:
    from . import db as D
    if not D.postgres():
        return {"skipped": "not on Postgres"}
    path = path or core.DATA_DIR / "app.db"
    if not path.exists():
        return {"skipped": f"no {path}"}
    c = core.db()
    if not force and c.execute("SELECT 1 FROM kv WHERE key = ?", (MARKER,)).fetchone():
        return {"skipped": "already copied"}
    _ensure_all_tables()
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    copied = {}
    try:
        names = [r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        names = [n for n in FIRST if n in names] + [n for n in names if n not in FIRST]
        with D.ddl_tx() as pg:             # as the database owner (sequences), into the first workspace
            have = {r[0] for r in pg.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")}
            for t in names:
                if t not in have:
                    continue
                pcols = [r[0] for r in pg.execute("SELECT column_name FROM information_schema.columns WHERE "
                                                  "table_schema = current_schema() AND table_name = ?", (t,))]
                scols = [r[1] for r in src.execute(f'PRAGMA table_info("{t}")')]
                cols = [x for x in scols if x in pcols]
                if not cols:
                    continue
                n = 0
                collist = ", ".join(f'"{x}"' for x in cols)
                ph = ", ".join("?" for _ in cols)
                for r in src.execute(f'SELECT {collist} FROM "{t}"'):
                    n += pg.execute(f'INSERT INTO "{t}" ({collist}) VALUES ({ph}) ON CONFLICT DO NOTHING', tuple(r)).rowcount
                copied[t] = n
                if "id" in pcols:
                    seq = pg.execute("SELECT pg_get_serial_sequence(?, 'id')", (t,)).fetchone()[0]
                    if seq:
                        pg.execute(f'SELECT setval(?, GREATEST((SELECT COALESCE(max(id), 0) FROM "{t}"), 1))', (seq,))
            pg.execute(f"INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT {D.ck('key')} DO UPDATE SET value = excluded.value",
                       (MARKER, json.dumps({"from": str(path), "time": core.now(), "rows": copied})))
    finally:
        src.close()
    print(f"Copied {path} into Postgres: {copied}", flush=True)
    return {"copied": copied}


def migrate_once():
    """Called from core.db() on Postgres: copy var/app.db the first time (per process and database)."""
    key = core.db_key() + "|" + str(core.DATA_DIR)
    if key in _done:
        return
    with _lock:
        if key in _done:
            return
        _done.add(key)                         # before migrating: migrate() itself uses the database
        migrate()


if __name__ == "__main__":
    import sys
    print(migrate(force="--force" in sys.argv))
