"""Row rules for numbers (0.9): metrics only count the rows a person may see.

A rule says: for <entity>, people in <audience> see only rows whose <column> is one of <values>. Rows of other
tables that join to hidden rows (orders of customers in hidden regions) are hidden too. The audience is
a data set (everyone who can see it), a team (its members and the members of the teams under it) or a role (whoever
has it, directly or through their team). An entity with rules is closed to callers matching none of them; entities
without rules are not filtered. Company admins and unrestricted keys see every row.

Rules are managed on the Access tab (per data set, team or role) and stored in the app database (table
sl_row_rules, per workspace). Sources may add rules of their own (source = source:<id>), replaced as a set.

The caller of the current MCP call / chat turn is in SCOPE (set by app/mcp_server.run): {"groups", "teams", "roles"}
or None (no restriction: company admins, the command line, quality runs).
"""
import contextvars
import json

KINDS = ("dataset", "team", "role")
SCHEMA = """CREATE TABLE IF NOT EXISTS sl_row_rules (
  entity TEXT NOT NULL, col TEXT NOT NULL, kind TEXT NOT NULL, audience TEXT NOT NULL, vals TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'manual', updated TEXT NOT NULL, by_user TEXT{tenant},
  PRIMARY KEY ({tk}entity, col, kind, audience))"""
SCOPE: contextvars.ContextVar[dict | None] = contextvars.ContextVar("cl_row_scope", default=None)


def _db():
    from app import core
    return core.db_with(SCHEMA)


def rules() -> list[dict]:
    return [{"entity": r["entity"], "column": r["col"], "kind": r["kind"], "audience": r["audience"],
             "values": json.loads(r["vals"]), "source": r["source"], "updated": r["updated"], "by": r["by_user"]}
            for r in _db().execute("SELECT * FROM sl_row_rules ORDER BY entity, kind, audience").fetchall()]


def put_rule(entity: str, column: str, kind: str, audience: str, values: list, source: str = "manual", by: str = ""):
    from app import core
    from app import db as D
    if kind not in KINDS:
        raise ValueError(f"A rule is for a {', '.join(KINDS)}.")
    vals = sorted({str(v).strip() for v in values if str(v).strip()})
    if not vals:
        raise ValueError("Give at least one value the rows may have.")
    if not entity or not column or not audience:
        raise ValueError("Give the table, the column and who the rule is for.")
    _db().execute(f"INSERT INTO sl_row_rules (entity, col, kind, audience, vals, source, updated, by_user) "
                  f"VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT {D.ck('entity', 'col', 'kind', 'audience')} DO UPDATE SET "
                  "vals = excluded.vals, source = excluded.source, updated = excluded.updated, by_user = excluded.by_user",
                  (entity, column, kind, audience, json.dumps(vals), source, core.now(), by))


def delete_rule(entity: str, column: str, kind: str, audience: str) -> bool:
    return _db().execute("DELETE FROM sl_row_rules WHERE entity = ? AND col = ? AND kind = ? AND audience = ?",
                         (entity, column, kind, audience)).rowcount > 0


def replace_rules(new: list[dict], source: str):
    """Rules owned by one source are replaced as a set; other rules stay."""
    _db().execute("DELETE FROM sl_row_rules WHERE source = ?", (source,))
    for r in new:
        put_rule(r["entity"], r["column"], r.get("kind", "dataset"), r["audience"], r["values"], source)


def scope_for(agent: dict) -> dict | None:
    """The row scope of an MCP caller: None for company admins and unrestricted keys."""
    if agent.get("all_groups") or agent.get("groups") is None:
        return None
    teams, roles = [], []
    if agent.get("kind") == "user" and agent.get("email"):
        try:
            from app import access
            e = access.effective(agent["email"], agent.get("role"))
            teams = list(e.get("teams") or [])
            roles = list(e.get("roles") or [])
            for t in teams:
                roles += [r for r in access.team_grant(t)["roles"] if r not in roles]
        except Exception:                                  # noqa: BLE001 - no access tables: data sets only
            pass
    return {"groups": list(agent.get("groups") or []), "teams": teams, "roles": roles}


def row_filters(scope: dict | None) -> dict[str, list[tuple[str, list]]] | None:
    """entity -> [(column, allowed values)] for this caller; None = no restriction. An entity with rules but none
    for the caller maps to [] (no rows)."""
    if scope is None:
        return None
    groups, teams, roles = set(scope.get("groups") or []), set(scope.get("teams") or []), set(scope.get("roles") or [])
    out: dict[str, dict[str, set]] = {}
    for r in rules():
        cols = out.setdefault(r["entity"], {})
        hit = (r["kind"] == "dataset" and r["audience"] in groups) or (r["kind"] == "team" and r["audience"] in teams) \
            or (r["kind"] == "role" and r["audience"] in roles)
        if hit:
            cols.setdefault(r["column"], set()).update(r["values"])
    return {e: [(c, sorted(v)) for c, v in cols.items()] for e, cols in out.items()}


def restrict(con, scope: dict | None, model=None):
    """Remove from this caller's DuckDB connection (an in-memory copy, see semantic/db.connect) the rows they may
    not see. Live database tables (views) are wrapped in a filtering view instead."""
    try:
        filters = row_filters(scope)
    except Exception:                                      # noqa: BLE001 - no app database: no rules
        filters = None
    if not filters:
        return con
    if model is None:
        from .yamlio import load_model
        model = load_model()
    tables = {e.name: e.table for e in model.entities}
    kinds = {r[0]: r[1] for r in con.execute("SELECT table_name, table_type FROM information_schema.tables").fetchall()}
    for entity, conds in filters.items():
        table = tables.get(entity)
        if not table or table not in kinds:
            continue
        if not conds:
            keep = "false"                                  # rules exist, none for this caller: no rows
        else:
            parts = []
            for col, vals in conds:
                lits = ", ".join("'" + str(v).replace("'", "''") + "'" for v in vals) or "NULL"
                parts.append(f'CAST("{col}" AS VARCHAR) IN ({lits})')
            keep = " AND ".join(parts)
        if kinds[table] == "VIEW":
            con.execute(f'ALTER VIEW "{table}" RENAME TO "{table}__all"')
            con.execute(f'CREATE VIEW "{table}" AS SELECT * FROM "{table}__all" WHERE {keep}')
        else:
            con.execute(f'DELETE FROM "{table}" WHERE NOT COALESCE(({keep}), false)')
    _cascade(con, model, tables, kinds, set(filters))
    return con


def _cascade(con, model, tables: dict, kinds: dict, restricted: set):
    """Rows that join (many-to-one) to hidden rows are hidden too: a rule on regions also hides the customers of other
    regions and their orders, so a total can never include them (not even as an empty region)."""
    joins = list(getattr(model, "joins", []) or [])
    todo = set(restricted)
    while todo:
        target = todo.pop()
        for j in joins:
            if j.to_entity != target or j.from_entity == target:
                continue
            src, dst = tables.get(j.from_entity), tables.get(j.to_entity)
            if not src or not dst or kinds.get(src) != "BASE TABLE":
                continue
            n = con.execute(f'SELECT count(*) FROM "{src}" s WHERE s."{j.from_column}" IS NOT NULL AND NOT EXISTS '
                            f'(SELECT 1 FROM "{dst}" d WHERE CAST(d."{j.to_column}" AS VARCHAR) = '
                            f'CAST(s."{j.from_column}" AS VARCHAR))').fetchone()[0]
            if n:
                con.execute(f'DELETE FROM "{src}" WHERE "{j.from_column}" IS NOT NULL AND NOT EXISTS '
                            f'(SELECT 1 FROM "{dst}" d WHERE CAST(d."{j.to_column}" AS VARCHAR) = '
                            f'CAST("{src}"."{j.from_column}" AS VARCHAR))')
                todo.add(j.from_entity)
