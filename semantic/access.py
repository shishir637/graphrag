"""Row access for metrics (table sl_access_rules): callers in a group only see the rows of an entity whose column
has one of the rule's values; an entity that has rules is closed to callers matching none of them. GitHub
connectors add one rule per repository (repo_id), an admin can add rules by hand (e.g. region managers).

Callers with every group (owners, admins, unrestricted keys) are not filtered.
"""
import json


def rules() -> list[dict]:
    from app import core
    t = core.current_tenant()
    if not t:
        return []
    return [{"entity": r["entity"], "column": r["col"], "group": r["grp"], "values": r["vals"], "source": r["source"]}
            for r in core.db().execute("SELECT * FROM sl_access_rules WHERE tenant_id = ? ORDER BY entity, grp", (t,))]


def put_rule(entity: str, column: str, group: str, values: list, source: str = "manual"):
    from app import core
    core.db().execute("INSERT INTO sl_access_rules (tenant_id, entity, col, grp, vals, source, updated) "
                      "VALUES (?, ?, ?, ?, ?::jsonb, ?, ?) ON CONFLICT (tenant_id, entity, col, grp) DO UPDATE SET "
                      "vals = excluded.vals, source = excluded.source, updated = excluded.updated",
                      (core.current_tenant(), entity, column, group, json.dumps(values), source, core.now()))


def delete_rule(entity: str, column: str, group: str) -> bool:
    from app import core
    return core.db().execute("DELETE FROM sl_access_rules WHERE tenant_id = ? AND entity = ? AND col = ? AND grp = ?",
                             (core.current_tenant(), entity, column, group)).rowcount > 0


def replace_rules(new: list[dict], source: str):
    """Rules owned by one source (e.g. a connector) are replaced as a set; other rules stay."""
    from app import core
    with core.tx() as c:
        c.execute("DELETE FROM sl_access_rules WHERE tenant_id = ? AND source = ?", (core.current_tenant(), source))
    for r in new:
        put_rule(r["entity"], r["column"], r["group"], r["values"], source)


def row_filters(groups: list[str] | None) -> dict[str, list[tuple[str, list]]] | None:
    """entity -> [(column, allowed values)] for this caller; None = no restriction. An entity with rules but none
    for the caller's groups maps to [] (no rows)."""
    if groups is None:
        return None
    out: dict[str, dict[str, set]] = {}
    for r in rules():
        cols = out.setdefault(r["entity"], {})
        if r["group"] in groups:
            cols.setdefault(r["column"], set()).update(map(str, r["values"]))
    return {e: [(c, sorted(v)) for c, v in cols.items()] for e, cols in out.items()}
