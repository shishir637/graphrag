"""Access groups of documents. Inside a workspace they are stored in Postgres (table documents); on the command
line in documents/_access.json ({"file.pdf": ["hr", "admin"]}). Default group: public."""
import json


def _ws():
    from .wspath import current_workspace
    return current_workspace()


def access_map() -> dict:
    t = _ws()
    if t:
        from app.db import db
        return {r["name"]: json.loads(r["groups"])
                for r in db().execute("SELECT name, groups FROM documents WHERE tenant_id = ?", (t,))}
    from .config import DOCS_DIR
    path = DOCS_DIR / "_access.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}


def set_groups(name: str, groups: list[str], source: str = "upload", source_ref: str = ""):
    t = _ws()
    if t:
        from app.core import now
        from app.db import db
        db().execute("INSERT INTO documents (tenant_id, name, groups, source, source_ref, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                     "ON CONFLICT (tenant_id, name) DO UPDATE SET groups = excluded.groups, updated_at = excluded.updated_at",
                     (t, name, json.dumps(groups), source, source_ref, now()))
        return
    m = access_map()
    m[name] = groups
    _save_file(m)


def forget(name: str):
    t = _ws()
    if t:
        from app.db import db
        db().execute("DELETE FROM documents WHERE tenant_id = ? AND name = ?", (t, name))
        return
    m = access_map()
    m.pop(name, None)
    _save_file(m)


def sources() -> dict[str, dict]:
    """name -> {source, source_ref} (where each document came from)."""
    t = _ws()
    if not t:
        return {}
    from app.db import db
    return {r["name"]: {"source": r["source"], "source_ref": r["source_ref"]}
            for r in db().execute("SELECT name, source, source_ref FROM documents WHERE tenant_id = ?", (t,))}


def _save_file(m: dict):
    from .config import DOCS_DIR
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    path = DOCS_DIR / "_access.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
