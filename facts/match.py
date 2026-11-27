"""Link facts that name something (contract parties, invoice vendors) to rows of the workspace's tables, so a
contract read from a PDF joins with the vendor master in the database (no AI model).

Candidates: every entity of the semantic model with a name-like dimension (name, title, label), except the fact
tables themselves. A match is found by
  - an id of that table mentioned in the passage (e.g. "V-4471")                                 -> score 100
  - the normalised name (lower case, punctuation and legal suffixes such as Pvt Ltd, LLP, Inc removed),
    compared with RapidFuzz token-sort ratio                                                     -> score 0-100
Links scoring 92+ are approved; 75-92 go to the owner's inbox; lower ones are not stored.
"""
import re

from rapidfuzz import fuzz, process

FIELDS = {("contract", "party_1"), ("contract", "party_2"), ("invoice", "vendor")}
SUFFIX = re.compile(r"\b(private|pvt|limited|ltd|llp|llc|inc|incorporated|corp|corporation|co|company|plc|gmbh|sa|ag|"
                    r"bv|pte|pty|the)\b\.?", re.I)
AUTO, KEEP = 92, 75


def norm_name(s: str) -> str:
    s = SUFFIX.sub(" ", s.lower())
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip()


def _candidates() -> list[dict]:
    from semantic.db import connect
    from semantic.yamlio import load_model
    out = []
    try:
        model, con = load_model(), connect()
    except Exception:                                      # noqa: BLE001 - no data layer
        return out
    for e in model.entities:
        if e.table.startswith("fact_"):
            continue
        lab = next((d.column for d in e.dimensions if d.type in ("text", "category")
                    and re.search(r"name|title|label", d.name)), None)
        if not lab:
            continue
        try:
            rows = con.execute(f'SELECT CAST("{e.primary_key}" AS VARCHAR), CAST("{lab}" AS VARCHAR) FROM "{e.table}" '
                               f'WHERE "{lab}" IS NOT NULL LIMIT 20000').fetchall()
        except Exception:                                  # noqa: BLE001
            continue
        out.append({"entity": e.name, "rows": rows, "norm": [norm_name(r[1]) for r in rows]})
    return out


def best_match(name: str, passage: str, cands: list[dict]) -> dict | None:
    best = None
    for c in cands:
        for key, label in c["rows"]:                       # an id written in the passage
            if key and len(key) >= 3 and re.search(rf"(?<![\w-]){re.escape(key)}(?![\w-])", passage):
                hit = {"entity": c["entity"], "row_key": key, "label": label, "score": 100.0, "method": "id in text"}
                if norm_name(label) and fuzz.token_sort_ratio(norm_name(label), norm_name(name)) >= 60:
                    return hit
                best = best or hit
        q = norm_name(name)
        if not q:
            continue
        m = process.extractOne(q, c["norm"], scorer=fuzz.token_sort_ratio)
        if m and (best is None or m[1] > best["score"]):
            key, label = c["rows"][m[2]]
            best = {"entity": c["entity"], "row_key": key, "label": label, "score": round(float(m[1]), 1),
                    "method": "name"}
    return best


def link_all(verbose: bool = True) -> int:
    """(Re)match the workspace's approved party / vendor facts; owners' decisions are kept."""
    from app import core
    t = core.current_tenant()
    cands = _candidates()
    if not cands:
        return 0
    facts = core.db().execute("SELECT f.id, f.kind, f.field, f.norm, f.chunk_id, f.quote FROM facts f WHERE f.tenant_id = ? "
                              "AND f.status = 'approved'", (t,)).fetchall()
    from rag import passages
    texts = passages.texts([f["chunk_id"] for f in facts])
    decided = {r["fact_id"] for r in core.db().execute("SELECT fact_id FROM fact_links WHERE tenant_id = ? AND decided_by IS NOT NULL",
                                                       (t,))}
    n = 0
    for f in facts:
        if (f["kind"], f["field"]) not in FIELDS or f["id"] in decided or not isinstance(f["norm"], str):
            continue
        m = best_match(f["norm"], texts.get(f["chunk_id"], f["quote"]), cands)
        if not m or m["score"] < KEEP:
            core.db().execute("DELETE FROM fact_links WHERE tenant_id = ? AND fact_id = ?", (t, f["id"]))
            continue
        st = "approved" if m["score"] >= AUTO else "needs_review"
        core.db().execute("INSERT INTO fact_links (tenant_id, fact_id, entity, row_key, label, score, method, status, updated) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (tenant_id, fact_id) DO UPDATE SET entity = excluded.entity, "
                          "row_key = excluded.row_key, label = excluded.label, score = excluded.score, method = excluded.method, "
                          "status = excluded.status, updated = excluded.updated",
                          (t, f["id"], m["entity"], m["row_key"], m["label"], m["score"], m["method"], st, core.now()))
        n += 1
    if verbose:
        print(f"  linked {n} names in facts to database rows")
    return n


def links() -> dict[str, dict]:
    """fact id -> approved link."""
    from app import core
    return {r["fact_id"]: {k: r[k] for k in ("entity", "row_key", "label", "score", "method")}
            for r in core.db().execute("SELECT * FROM fact_links WHERE tenant_id = ? AND status = 'approved'",
                                       (core.current_tenant(),))}
