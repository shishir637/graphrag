"""Compile a SemanticQuery (metrics + dimensions + filters + dates) into SQL, deterministically.

The LLM never writes SQL. It only picks metric and dimension names; this compiler turns them into SQL
using the reviewed definitions, so 'net revenue' always means exactly the same calculation.

  - metric formulas and always-on filters come from metrics.yaml; each metric's filters are applied
    to its own aggregates with FILTER (WHERE ...), so metrics with different rules can share one query
  - joins come from the model's join graph (networkx), only in the many-to-one direction, so totals
    are never inflated by duplicated rows
  - metrics counted on different entities (orders vs. order_items) are computed in separate
    sub-queries and joined on the requested dimensions
  - SQLGlot parses everything, quotes filter values safely, and pretty-prints the result
"""
import json
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from .model import join_path
from .schema import Metric, MetricSet, SemanticModel, SemanticQuery

D = "duckdb"
GRAINS = {
    "day": "CAST({c} AS DATE)",
    "week": "CAST(date_trunc('week', {c}) AS DATE)",
    "month": "CAST(date_trunc('month', {c}) AS DATE)",
    "quarter": "CAST(date_trunc('quarter', {c}) AS DATE)",
    "year": "year({c})",
    # Indian fiscal year, April-March: 2025-04-01 .. 2026-03-31 -> 'FY2025-26'
    "fiscal_year": "'FY' || CAST(year({c} - INTERVAL 3 MONTH) AS VARCHAR) || '-' || "
                   "right(CAST(year({c} - INTERVAL 3 MONTH) + 1 AS VARCHAR), 2)",
}


class CompileError(ValueError):
    pass


@dataclass
class Compiled:
    sql: str
    metrics: list[str]
    dimensions: list[str]                      # output column names
    joins: list[str] = field(default_factory=list)


def columns_in(sql: str) -> set[tuple[str, str]]:
    return {(c.table, c.name) for c in sqlglot.parse_one(sql, read=D).find_all(exp.Column)}


def _with_filters(expression: str, filters: list[str]) -> str:
    """sum(orders.amount) + filter "orders.status != 'cancelled'"
       -> SUM(orders.amount) FILTER(WHERE orders.status <> 'cancelled')"""
    tree = sqlglot.parse_one(expression, read=D)
    if not filters:
        return tree.sql(dialect=D)
    cond = sqlglot.parse_one(filters[0] if len(filters) == 1 else " AND ".join(f"({f})" for f in filters), read=D)

    def wrap(node):
        if isinstance(node, exp.AggFunc) and not isinstance(node.parent, exp.Filter):
            return exp.Filter(this=node.copy(), expression=exp.Where(this=cond.copy()))
        return node
    return tree.transform(wrap).sql(dialect=D)


def metric_sql(name: str, metrics: MetricSet, _seen: tuple = ()) -> str:
    """SQL expression for one metric (without the cumulative window)."""
    if name in _seen:
        raise CompileError(f"metric '{name}' refers to itself")
    m = metrics.get(name)
    if m.type in ("simple", "cumulative"):
        return _with_filters(m.expression, m.filters)
    if m.type == "ratio":
        num = metric_sql(m.numerator, metrics, _seen + (name,))
        den = metric_sql(m.denominator, metrics, _seen + (name,))
        for part in (m.numerator, m.denominator):
            if metrics.get(part).entity != m.entity:
                raise CompileError(f"ratio '{name}': '{part}' is counted on a different entity")
        return f"CAST({num} AS DOUBLE) / NULLIF({den}, 0)"
    # derived: "{net_revenue} / NULLIF({active_customers}, 0)"
    out = m.expression
    for other in metrics.metrics:
        token = "{" + other.name + "}"
        if token in out:
            if other.entity != m.entity:
                raise CompileError(f"derived '{name}': '{other.name}' is counted on a different entity")
            out = out.replace(token, f"({metric_sql(other.name, metrics, _seen + (name,))})")
    if "{" in out:
        raise CompileError(f"derived '{name}' refers to an unknown metric: {out}")
    return sqlglot.parse_one(out, read=D).sql(dialect=D)


def _metric_time_column(name: str, metrics: MetricSet) -> str | None:
    m = metrics.get(name)
    if m.time_column:
        return m.time_column
    for part in (m.numerator, m.denominator):
        if part and metrics.get(part).time_column:
            return metrics.get(part).time_column
    return None


@dataclass
class _Dim:
    ref: str            # as requested, e.g. "orders.order_date__month"
    entity: str
    column: str         # "orders.order_date"
    sql: str
    alias: str


def resolve_dimension(ref: str, model: SemanticModel) -> _Dim:
    if "." not in ref:
        raise CompileError(f"dimension '{ref}' must be written as entity.dimension")
    ent_name, rest = ref.split(".", 1)
    dim_name, _, grain = rest.partition("__")
    try:
        ent = model.entity(ent_name)
    except KeyError:
        raise CompileError(f"unknown entity '{ent_name}' in '{ref}'") from None
    d = ent.dimension(dim_name)
    if not d:
        raise CompileError(f"unknown dimension '{dim_name}' on '{ent_name}'. "
                           f"Available: {', '.join(x.name for x in ent.dimensions)}")
    col = f"{ent_name}.{d.column}"
    if grain:
        if d.type != "time":
            raise CompileError(f"'{ref}': only time dimensions take a grain")
        if grain not in GRAINS:
            raise CompileError(f"'{ref}': grain must be one of {sorted(GRAINS)}")
        return _Dim(ref, ent_name, col, GRAINS[grain].format(c=col), f"{dim_name}_{grain}")
    return _Dim(ref, ent_name, col, col, dim_name)


def _literal(v) -> str:
    return exp.convert(v).sql(dialect=D)


def _filter_sql(f, model) -> tuple[str, str]:
    d = resolve_dimension(f.dimension, model)
    vals = [_literal(v) for v in f.values]
    if f.op in ("in", "not in") or (f.op in ("=", "!=") and len(vals) > 1):
        op = "NOT IN" if f.op in ("not in", "!=") else "IN"
        return d.entity, f"{d.sql} {op} ({', '.join(vals)})"
    if len(vals) != 1:
        raise CompileError(f"filter on '{f.dimension}' with '{f.op}' takes one value")
    return d.entity, f"{d.sql} {f.op} {vals[0]}"


def _compile_group(base: str, names: list[str], dims: list[_Dim], q: SemanticQuery,
                   model: SemanticModel, metrics: MetricSet, joins_used: list[str]) -> str:
    exprs = {n: metric_sql(n, metrics) for n in names}

    # cumulative metrics: running total over the requested time dimension
    for n in names:
        m = metrics.get(n)
        if m.type == "cumulative":
            tdim = next((d for d in dims if d.column == m.time_column and d.sql != d.column), None)
            if not tdim:
                raise CompileError(f"'{n}' is a running total: add a time dimension on {m.time_column}, "
                                   f"e.g. '{m.time_column}__month'")
            part = [d.sql for d in dims if d is not tdim]
            over = (f"PARTITION BY {', '.join(part)} " if part else "") + f"ORDER BY {tdim.sql}"
            exprs[n] = f"SUM({exprs[n]}) OVER ({over})"

    where, needed = [], set()
    for sql in exprs.values():
        needed |= {t for t, _ in columns_in(sql) if t}
    needed |= {d.entity for d in dims}
    for f in q.filters:
        ent, cond = _filter_sql(f, model)
        needed.add(ent)
        where.append(cond)
    tcols = {_metric_time_column(n, metrics) for n in names} - {None}
    if (q.start_date or q.end_date) and tcols:
        if len(tcols) > 1:
            raise CompileError(f"metrics {names} use different time columns {sorted(tcols)}")
        tcol = tcols.pop()
        needed.add(tcol.split(".")[0])
        if q.start_date:
            where.append(f"{tcol} >= DATE '{q.start_date.isoformat()}'")
        if q.end_date:
            where.append(f"{tcol} <= DATE '{q.end_date.isoformat()}'")

    joins, seen = [], set()
    for ent in sorted(needed - {base}):
        try:
            path = join_path(model, base, ent)
        except ValueError as e:
            raise CompileError(str(e)) from None
        for a, b, j in path:
            if b in seen:
                continue
            seen.add(b)
            joins.append(f'LEFT JOIN "{model.entity(b).table}" AS {b} '
                         f"ON {j.from_entity}.{j.from_column} = {j.to_entity}.{j.to_column}")
            joins_used.append(f"{a} -> {b} ({j.from_entity}.{j.from_column} = {j.to_entity}.{j.to_column})")

    select = [f"{d.sql} AS {d.alias}" for d in dims] + [f"{e} AS {n}" for n, e in exprs.items()]
    sql = f'SELECT {", ".join(select)} FROM "{model.entity(base).table}" AS {base} ' + " ".join(joins)
    if where:
        sql += " WHERE " + " AND ".join(where)
    if dims:
        sql += " GROUP BY " + ", ".join(str(i) for i in range(1, len(dims) + 1))
    return sql


def compile_query(q: SemanticQuery, model: SemanticModel, metrics: MetricSet) -> Compiled:
    for n in q.metrics:
        try:
            metrics.get(n)
        except KeyError:
            raise CompileError(f"unknown metric '{n}'. Available: {', '.join(m.name for m in metrics.metrics)}") from None
    dims = [resolve_dimension(r, model) for r in q.dimensions]
    aliases = [d.alias for d in dims]
    for d in dims:                                        # make output names unique
        if aliases.count(d.alias) > 1:
            d.alias = f"{d.entity}_{d.alias}"
    out_names = [d.alias for d in dims]

    groups: dict[str, list[str]] = {}
    for n in q.metrics:
        groups.setdefault(metrics.get(n).entity, []).append(n)
    joins_used: list[str] = []
    parts = [(_compile_group(base, names, dims, q, model, metrics, joins_used)) for base, names in groups.items()]

    if len(parts) == 1:
        sql = parts[0]
    else:   # metrics on different entities: one sub-query each, joined on the dimensions
        ctes = ", ".join(f"g{i} AS ({p})" for i, p in enumerate(parts))
        body = "g0"
        for i in range(1, len(parts)):
            if out_names:   # NULL-safe, so rows with an unknown value (e.g. missing region) still line up
                on = " AND ".join(f"g0.{a} IS NOT DISTINCT FROM g{i}.{a}" for a in out_names)
                body += f" FULL OUTER JOIN g{i} ON {on}"
            else:
                body += f" CROSS JOIN g{i}"
        dim_cols = [f"COALESCE({', '.join(f'g{i}.{a}' for i in range(len(parts)))}) AS {a}" for a in out_names]
        sql = f"WITH {ctes} SELECT {', '.join(dim_cols + list(q.metrics))} FROM {body}"

    if q.limit and out_names:
        # rankings ("top 5 cities", "region with the fewest orders") leave out the unknown group: rows whose
        # dimension is empty (e.g. orders of deleted customers) are not a real city/region and must not win
        sql = (f"SELECT * FROM ({sql}) AS ranked WHERE "
               + " AND ".join(f"{a} IS NOT NULL" for a in out_names))

    allowed = set(out_names) | set(q.metrics)
    order = []
    for o in q.order_by:
        name = o.lstrip("-")
        if name not in allowed:
            raise CompileError(f"order_by '{name}' must be one of {sorted(allowed)}")
        order.append(f"{name} {'DESC' if o.startswith('-') else 'ASC'} NULLS LAST")
    if not order and out_names:
        order = [f"{a} ASC NULLS LAST" for a in out_names]
    if order:
        sql += " ORDER BY " + ", ".join(order)
    if q.limit:
        sql += f" LIMIT {int(q.limit)}"

    try:
        pretty = sqlglot.transpile(sql, read=D, write=D, pretty=True)[0]
    except sqlglot.errors.ParseError as e:
        raise CompileError(f"generated SQL does not parse: {e}") from None
    return Compiled(pretty, list(q.metrics), out_names, sorted(set(joins_used)))


def run(q: SemanticQuery, con=None, model=None, metrics=None):
    """Compile and execute on DuckDB. Returns (Compiled, pandas DataFrame)."""
    from .db import connect
    from .yamlio import load_metrics, load_model
    model, metrics = model or load_model(), metrics or load_metrics()
    c = compile_query(q, model, metrics)
    con = con or connect()
    return c, con.execute(c.sql).df()


def run_json(text: str):
    q = SemanticQuery.model_validate(json.loads(text))
    c, df = run(q)
    print(c.sql, "\n")
    print(df.to_string(index=False, max_rows=40))
