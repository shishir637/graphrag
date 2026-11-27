"""Passages (chunks) of the documents: the system of record for document text.

Inside a workspace they live in Postgres (table passages, migrations/012_passages.sql); on the command line
(research mode, no workspace) in outputs/2_chunks.json, as before. Every reader goes through this module, so it
never matters where they are.

A passage is a dict with the keys the pipeline has always used: chunk_id, source, access, headings, pages, text,
embed_text, n_tokens (plus position, its place in the document).

save_all() replaces the workspace's passages with a new chunking: unchanged passages (same id and text hash) are
not touched, changed ones are updated, removed ones deleted; the version (passages_state) moves only when
something changed. Caches key on stamp(), which is that version (or the file's modification time on the command
line).
"""
import hashlib
import json

from . import config

FILE = "2_chunks.json"


def _tenant() -> str | None:
    from .wspath import current_workspace
    return current_workspace()


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------ command line: the old file

def _file_rows() -> list[dict]:
    p = config.OUT_DIR / FILE
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def _file_mtime() -> float | None:
    try:
        return (config.OUT_DIR / FILE).stat().st_mtime
    except OSError:
        return None


# ------------------------------------------------------------------ the repository

def _row(r) -> dict:
    return {"chunk_id": r["chunk_id"], "source": r["source"], "position": r["position"],
            "access": r["access"] if isinstance(r["access"], list) else json.loads(r["access"]),
            "headings": r["headings"] if isinstance(r["headings"], list) else json.loads(r["headings"]),
            "pages": r["pages"] if isinstance(r["pages"], list) else json.loads(r["pages"]),
            "text": r["text"], "embed_text": r["embed_text"] or r["text"], "n_tokens": r["n_tokens"]}


def exists() -> bool:
    t = _tenant()
    if not t:
        return (config.OUT_DIR / FILE).exists()
    from app.db import db
    return db().execute("SELECT 1 FROM passages WHERE tenant_id = ? LIMIT 1", (t,)).fetchone() is not None


def all() -> list[dict]:                                     # noqa: A001 - the module's natural name
    """Every passage, in document order."""
    t = _tenant()
    if not t:
        return _file_rows()
    from app.db import db
    return [_row(r) for r in db().execute("SELECT * FROM passages WHERE tenant_id = ? ORDER BY source, position", (t,))]


def by_ids(ids) -> dict[str, dict]:
    """chunk_id -> passage, in one query."""
    ids = list(dict.fromkeys(i for i in ids if i))
    if not ids:
        return {}
    t = _tenant()
    if not t:
        want = set(ids)
        return {c["chunk_id"]: c for c in _file_rows() if c["chunk_id"] in want}
    from app.db import db
    out = {}
    for i in range(0, len(ids), 1000):
        for r in db().execute("SELECT * FROM passages WHERE tenant_id = ? AND chunk_id = ANY(?)", (t, ids[i:i + 1000])):
            out[r["chunk_id"]] = _row(r)
    return out


def texts(ids) -> dict[str, str]:
    """chunk_id -> text, in one query (search fetches the texts of its candidates with this)."""
    ids = list(dict.fromkeys(i for i in ids if i))
    if not ids:
        return {}
    t = _tenant()
    if not t:
        want = set(ids)
        return {c["chunk_id"]: c["text"] for c in _file_rows() if c["chunk_id"] in want}
    from app.db import db
    return {r["chunk_id"]: r["text"] for r in db().execute(
        "SELECT chunk_id, text FROM passages WHERE tenant_id = ? AND chunk_id = ANY(?)", (t, ids))}


def counts_by_source() -> dict[str, int]:
    t = _tenant()
    if not t:
        out: dict[str, int] = {}
        for c in _file_rows():
            out[c["source"]] = out.get(c["source"], 0) + 1
        return out
    from app.db import db
    return {r["source"]: r["n"] for r in db().execute(
        "SELECT source, count(*) AS n FROM passages WHERE tenant_id = ? GROUP BY source", (t,))}


def stamp():
    """Changes whenever the passages change: the workspace's version, or the file's modification time."""
    t = _tenant()
    if not t:
        return _file_mtime()
    from app.db import db
    r = db().execute("SELECT version FROM passages_state WHERE tenant_id = ?", (t,)).fetchone()
    return ("v", t, r["version"]) if r else None


def save_all(chunks: list[dict]) -> dict:
    """Replace the passages with a new chunking (unchanged ones untouched). Returns counts."""
    t = _tenant()
    if not t:
        from .utils import write_json
        write_json(FILE, chunks)
        return {"passages": len(chunks)}
    from app.core import now
    from app.db import tx
    ts = now()
    pos: dict[str, int] = {}
    rows = []
    for c in chunks:
        p = pos[c["source"]] = pos.get(c["source"], -1) + 1
        h = text_hash(c.get("embed_text") or c["text"])
        rows.append((c, p, h))
    out = {"passages": len(rows), "added": 0, "changed": 0, "unchanged": 0, "removed": 0}
    with tx() as cur:
        old = {r["chunk_id"]: (r["text_hash"], r["position"], r["access"]) for r in cur.execute(
            "SELECT chunk_id, text_hash, position, access FROM passages WHERE tenant_id = ?", (t,))}
        keep = set()
        for c, p, h in rows:
            keep.add(c["chunk_id"])
            access = sorted(c.get("access") or ["public"])
            prev = old.get(c["chunk_id"])
            if prev:
                acc = prev[2] if isinstance(prev[2], list) else json.loads(prev[2])
                if prev[0] == h and prev[1] == p and sorted(acc) == access:
                    out["unchanged"] += 1
                    continue
                out["changed"] += 1
            else:
                out["added"] += 1
            cur.execute(
                "INSERT INTO passages (tenant_id, chunk_id, source, position, headings, pages, text, embed_text, n_tokens, "
                "text_hash, access, created_at, updated_at) VALUES (?, ?, ?, ?, ?::jsonb, ?::jsonb, ?, ?, ?, ?, ?::jsonb, ?, ?) "
                "ON CONFLICT (tenant_id, chunk_id) DO UPDATE SET source = excluded.source, position = excluded.position, "
                "headings = excluded.headings, pages = excluded.pages, text = excluded.text, embed_text = excluded.embed_text, "
                "n_tokens = excluded.n_tokens, text_hash = excluded.text_hash, access = excluded.access, "
                "updated_at = excluded.updated_at",
                (t, c["chunk_id"], c["source"], p, json.dumps(c.get("headings") or []), json.dumps(c.get("pages") or []),
                 c["text"], c.get("embed_text") or c["text"], c.get("n_tokens"), h, json.dumps(access), ts, ts))
        gone = [cid for cid in old if cid not in keep]
        for i in range(0, len(gone), 1000):
            cur.execute("DELETE FROM passages WHERE tenant_id = ? AND chunk_id = ANY(?)", (t, gone[i:i + 1000]))
        out["removed"] = len(gone)
        if out["added"] or out["changed"] or out["removed"] or not old:
            cur.execute("INSERT INTO passages_state (tenant_id, version, updated_at) VALUES (?, 1, ?) ON CONFLICT (tenant_id) "
                        "DO UPDATE SET version = passages_state.version + 1, updated_at = excluded.updated_at", (t, ts))
    return out


def backfill() -> dict:
    """0.7 upgrade: passages of a workspace's old outputs/2_chunks.json moved into Postgres (the file is kept as
    2_chunks.json.pre-0.7 until the cleanup). Idempotent: nothing to do once the file has been moved."""
    t = _tenant()
    p = config.OUT_DIR / FILE
    if not t or not p.exists():
        return {"passages": 0}
    chunks = json.loads(p.read_text(encoding="utf-8"))
    r = save_all(chunks)
    p.replace(p.with_name(FILE + ".pre-0.7"))
    return {"passages": r["passages"]}
