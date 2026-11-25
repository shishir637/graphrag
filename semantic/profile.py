"""Phase 1 - Profiling: learn what is in the tables.

DuckDB runs the profiling queries straight on the files (counts, nulls, distinct values, min/max,
top values); pandas-style type guessing gives each column a role (key, measure, category, time, text);
plain SQL finds likely joins by checking what share of one column's values appear in another.

Output: outputs/sl_1_profile.json
"""
import re

from rag.utils import write_json

from .config import CATEGORY_MAX_DISTINCT, JOIN_MIN_CONTAINMENT
from .db import columns, connect

NUMERIC = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "FLOAT", "DOUBLE", "DECIMAL")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _role(col: str, dtype: str, n_rows: int, n_distinct: int, samples: list) -> tuple[str, str]:
    """(role, semantic_type). Roles drive the draft model: key -> join/ids, measure -> aggregations,
    category -> group-by dimension, time -> date dimension, text -> free text."""
    if dtype.startswith(("DATE", "TIMESTAMP")):
        return "time", "date"
    if col == "id" or col.endswith("_id"):
        return "key", "identifier"
    if dtype.startswith(NUMERIC):
        return "measure", "number"
    if samples and all(isinstance(s, str) and EMAIL.match(s) for s in samples):
        return "text", "email"
    if n_distinct <= CATEGORY_MAX_DISTINCT or (n_rows and n_distinct / n_rows < 0.05):
        return "category", "text"
    return "text", "text"


def profile_table(con, table: str) -> dict:
    t = _q(table)
    n_rows = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
    cols = {}
    for col, dtype in columns(con, table).items():
        c = _q(col)
        nulls, distinct, mn, mx = con.execute(
            f"SELECT count(*) - count({c}), count(DISTINCT {c}), min({c})::VARCHAR, max({c})::VARCHAR FROM {t}"
        ).fetchone()
        top = con.execute(
            f"SELECT {c}::VARCHAR, count(*) n FROM {t} WHERE {c} IS NOT NULL GROUP BY 1 ORDER BY n DESC, 1 LIMIT 5"
        ).fetchall()
        samples = [r[0] for r in con.execute(
            f"SELECT DISTINCT {c} FROM {t} WHERE {c} IS NOT NULL LIMIT 5").fetchall()]
        role, sem = _role(col, dtype, n_rows, distinct, samples)
        cols[col] = {
            "type": dtype, "role": role, "semantic_type": sem,
            "nulls": nulls, "null_pct": round(100 * nulls / n_rows, 2) if n_rows else 0,
            "distinct": distinct, "unique": distinct == n_rows - nulls and nulls == 0,
            "min": mn, "max": mx,
            "top_values": [{"value": v, "count": n} for v, n in top],
        }
        if role == "category":
            cols[col]["values"] = [r[0] for r in con.execute(
                f"SELECT DISTINCT {c}::VARCHAR FROM {t} WHERE {c} IS NOT NULL ORDER BY 1").fetchall()]
    pk = [c for c, p in cols.items() if p["role"] == "key" and p["unique"]]
    return {"rows": n_rows, "primary_key_guess": pk[0] if pk else None, "columns": cols}


def detect_joins(con, profiles: dict) -> list[dict]:
    """For each key-like column, check what share of its distinct values exist in a UNIQUE column of
    another table. e.g. 99.9% of orders.customer_id found in customers.customer_id -> many_to_one join."""
    found = []
    for a, pa in profiles.items():
        for ca, ia in pa["columns"].items():
            if ia["role"] != "key":
                continue
            for b, pb in profiles.items():
                if a == b:
                    continue
                for cb, ib in pb["columns"].items():
                    if not ib["unique"] or ib["type"] != ia["type"] or ib["role"] != "key":
                        continue
                    share = con.execute(
                        f"SELECT count(DISTINCT x.v) FILTER (WHERE y.v IS NOT NULL) / count(DISTINCT x.v) "
                        f"FROM (SELECT DISTINCT {_q(ca)} v FROM {_q(a)} WHERE {_q(ca)} IS NOT NULL) x "
                        f"LEFT JOIN (SELECT DISTINCT {_q(cb)} v FROM {_q(b)}) y ON x.v = y.v"
                    ).fetchone()[0] or 0
                    if share >= JOIN_MIN_CONTAINMENT:
                        found.append({
                            "from": f"{a}.{ca}", "to": f"{b}.{cb}", "containment": round(float(share), 4),
                            "relationship": "one_to_one" if ia["unique"] else "many_to_one",
                            "unmatched_values": ia["distinct"] - round(share * ia["distinct"]),
                        })
    return found


def profile_all() -> dict:
    con = connect()
    # the workspace's own tables and views (live database sources are views); not tables of attached catalogs
    tables = [r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_catalog = "
                                        "current_database() AND table_schema = 'main' ORDER BY table_name").fetchall()]
    profiles = {t: profile_table(con, t) for t in sorted(tables)}
    joins = detect_joins(con, profiles)
    result = {"tables": profiles, "suggested_joins": joins}
    write_json("sl_1_profile.json", result)
    for t, p in profiles.items():
        roles = {}
        for c, info in p["columns"].items():
            roles.setdefault(info["role"], []).append(c)
        print(f"  {t}: {p['rows']} rows, key={p['primary_key_guess']}, " +
              ", ".join(f"{r}={len(c)}" for r, c in sorted(roles.items())))
    for j in joins:
        print(f"  join? {j['from']} -> {j['to']}  ({j['containment']:.1%} found, {j['relationship']})")
    return result
