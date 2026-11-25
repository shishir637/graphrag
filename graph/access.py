"""Access groups of passages, shared by search, the graph and the MCP tools (no model, no database).

Every passage carries the groups of the document it came from (documents/_access.json, default "public").
A caller sees a passage when it shares at least one group with it; groups=None means no restriction
(the owner's own tools and the command line)."""
import json

_ACCESS = {"mtime": None, "map": {}, "groups": set()}


def chunk_access() -> dict:
    """chunk_id -> access groups, from the document pipeline's chunks file (reloaded when it changes)."""
    from rag.config import OUT_DIR as RAG_OUT
    p = RAG_OUT / "2_chunks.json"
    if not p.exists():
        return {}
    m = p.stat().st_mtime
    if _ACCESS["mtime"] != m:
        chunks = json.loads(p.read_text(encoding="utf-8"))
        _ACCESS.update(mtime=m, map={c["chunk_id"]: c.get("access", ["public"]) for c in chunks},
                       groups={g for c in chunks for g in c.get("access", ["public"])})
    return _ACCESS["map"]


def visible(chunk_id: str, groups: list[str] | None) -> bool:
    """None = no restriction (CLI / owner). Unknown chunks are hidden from restricted callers."""
    if groups is None:
        return True
    return bool(set(chunk_access().get(chunk_id, [])) & set(groups))


def sees_everything(groups: list[str] | None) -> bool:
    chunk_access()
    return groups is None or _ACCESS["groups"] <= set(groups)


def all_groups() -> set:
    chunk_access()
    return set(_ACCESS["groups"])
