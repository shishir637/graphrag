"""Access groups of passages, shared by search, the graph and the MCP tools (no model, no database).

Every passage carries the groups of the document it came from (rag/passages.py; default "public").
A caller sees a passage when it shares at least one group with it; groups=None means no restriction
(the owner's own tools and the command line)."""
from rag.wspath import PerFile

_ACCESS = PerFile()                    # per chunks file, i.e. per workspace


def chunk_access() -> dict:
    """chunk_id -> access groups, from the passages repository (reloaded when the passages change)."""
    return _entry()["map"]


def _entry() -> dict:
    from rag import passages
    from rag.config import OUT_DIR as RAG_OUT
    e = _ACCESS.entry(RAG_OUT / "2_chunks.json")              # cache key per workspace
    m = passages.stamp()
    if m is None:
        return {"mtime": None, "map": {}, "groups": set()}
    if e["mtime"] != m:
        chunks = passages.all()
        e.update(mtime=m, map={c["chunk_id"]: c.get("access", ["public"]) for c in chunks},
                 groups={g for c in chunks for g in c.get("access", ["public"])})
    return e


def visible(chunk_id: str, groups: list[str] | None) -> bool:
    """None = no restriction (CLI / owner). Unknown chunks are hidden from restricted callers."""
    if groups is None:
        return True
    return bool(set(chunk_access().get(chunk_id, [])) & set(groups))


def sees_everything(groups: list[str] | None) -> bool:
    return groups is None or _entry().get("groups", set()) <= set(groups)


def all_groups() -> set:
    return set(_entry().get("groups", set()))
