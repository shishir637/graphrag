"""Facts and doc cards of a workspace: run after the documents are chunked (engine.prepare_documents).

run():
  1. per document: kind (rules), facts per passage (facts/extract.py), validation (facts/validate.py: quote check,
     ontology constraints, matching to database rows) -> status approved / needs_review / rejected
  2. facts stored in Postgres (owners' decisions are kept across runs: same fact id = same document, record, field,
     value and passage)
  3. approved facts written as Parquet tables in the workspace's data folder (fact_contracts, fact_invoices,
     fact_meeting_decisions, fact_meeting_actions), so the semantic layer drafts them and DuckDB joins them with
     database tables; row access per document group (sl_access_rules, source "facts")
  4. one card per document (doc_cards)
Returns whether a fact table changed (then the data build runs too).
"""
import hashlib
import json
from collections import defaultdict

from .extract import card, extract, kind_of

TABLES = {
    "fact_contracts": ("contract", ["document", "title", "party_1", "party_1_key", "party_2", "party_2_key",
                                    "effective_date", "term_months",
                                    "notice_days", "annual_fee", "currency", "payment_terms_days", "governing_law",
                                    "auto_renew", "liability"]),
    "fact_invoices": ("invoice", ["document", "invoice_number", "invoice_date", "vendor", "vendor_key", "amount", "tax",
                                  "currency"]),
    "fact_meeting_decisions": ("meeting", ["document", "meeting_date", "decision"]),
    "fact_meeting_actions": ("meeting", ["document", "meeting_date", "owner", "task", "due_date"]),
}


def fact_id(tenant: str, document: str, f: dict) -> str:
    key = json.dumps([tenant, document, f["record"], f["field"], f["value"], f["chunk_id"]], ensure_ascii=False)
    return "f_" + hashlib.sha1(key.encode()).hexdigest()[:16]


def _db():
    from app import core
    return core


def run(verbose: bool = True) -> bool:
    from rag.config import OUT_DIR
    from rag.registry import access_map
    from .validate import validate_document
    core = _db()
    t = core.current_tenant()
    path = OUT_DIR / "2_chunks.json"
    if not t or not path.exists():
        return False
    chunks = json.loads(path.read_text(encoding="utf-8"))
    by_doc: dict[str, list] = defaultdict(list)
    for c in chunks:
        by_doc[c["source"]].append(c)
    groups = access_map()
    old = {r["id"]: r for r in core.db().execute("SELECT id, status, decided_by, value, norm FROM facts WHERE tenant_id = ?", (t,))}
    keep, n_facts, kinds = set(), 0, defaultdict(int)
    for doc, cs in by_doc.items():
        kind, _ = kind_of(doc, "\n".join(c["text"] for c in cs))
        kinds[kind] += 1
        facts = [f.as_dict() for c in cs for f in extract(kind, c)]
        facts = validate_document(doc, kind, cs, facts)
        for f in facts:
            fid = fact_id(t, doc, f)
            keep.add(fid)
            prev = old.get(fid)
            status, reason, by = f["status"], f.get("reason", ""), None
            value, norm = f["value"], f["norm"]
            if prev and prev["decided_by"]:                # an owner decided: keep it (and an edited value)
                status, by = prev["status"], prev["decided_by"]
                value, norm = prev["value"], prev["norm"]
            core.db().execute(
                "INSERT INTO facts (id, tenant_id, document, kind, record, field, value, norm, chunk_id, quote, span_start, "
                "span_end, extractor, confidence, status, reason, decided_by, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?::jsonb, "
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET value = excluded.value, norm = excluded.norm, "
                "quote = excluded.quote, span_start = excluded.span_start, span_end = excluded.span_end, "
                "confidence = excluded.confidence, status = excluded.status, reason = excluded.reason, "
                "decided_by = excluded.decided_by, updated = excluded.updated",
                (fid, t, doc, kind, f["record"], f["field"], value, json.dumps(norm, default=str), f["chunk_id"], f["quote"],
                 f["start"], f["end"], f["extractor"], f["confidence"], status, reason[:300], by, core.now()))
            n_facts += 1
        approved = [dict(f, norm=f["norm"]) for f in facts if (old.get(fact_id(t, doc, f)) or {}).get("status", f["status"]) == "approved"]
        c = card(doc, kind, cs, approved)
        c["groups"] = groups.get(doc, ["public"])
        core.db().execute("INSERT INTO doc_cards (tenant_id, name, kind, card, updated) VALUES (?, ?, ?, ?::jsonb, ?) "
                          "ON CONFLICT (tenant_id, name) DO UPDATE SET kind = excluded.kind, card = excluded.card, "
                          "updated = excluded.updated", (t, doc, kind, json.dumps(c, default=str), core.now()))
    gone = [fid for fid in old if fid not in keep]
    for i in range(0, len(gone), 500):
        core.db().execute("DELETE FROM facts WHERE tenant_id = ? AND id = ANY(?)", (t, gone[i:i + 500]))
    core.db().execute("DELETE FROM doc_cards WHERE tenant_id = ? AND NOT (name = ANY(?))", (t, list(by_doc)))
    core.db().execute("DELETE FROM fact_links WHERE tenant_id = ? AND NOT (fact_id = ANY(?))", (t, list(keep)))
    from .match import link_all
    link_all(verbose)                                    # names in facts -> rows of the workspace's tables
    changed = write_tables()
    if verbose:
        print(f"  facts: {n_facts} from {len(by_doc)} documents ({dict(kinds)}); "
              f"{'fact tables updated' if changed else 'fact tables unchanged'}")
    return changed


# ------------------------------------------------------------------ approved facts -> tables

def approved_rows() -> dict[str, list[dict]]:
    """One row per record of every approved fact, per fact table."""
    core = _db()
    from .match import links
    lk = links()
    rows = core.db().execute("SELECT id, document, kind, record, field, value, norm FROM facts WHERE tenant_id = ? AND "
                             "status = 'approved' ORDER BY document, record, field", (core.current_tenant(),)).fetchall()
    cards = {r["name"]: (r["card"] if isinstance(r["card"], dict) else json.loads(r["card"]))
             for r in core.db().execute("SELECT name, card FROM doc_cards WHERE tenant_id = ?", (core.current_tenant(),))}
    recs: dict[tuple, dict] = {}
    meet_date: dict[str, str] = {}
    for r in rows:
        norm = r["norm"]
        if r["kind"] == "meeting" and r["field"] == "meeting_date":
            meet_date[r["document"]] = norm
        if r["kind"] == "meeting" and r["field"] == "decision":
            recs[("fact_meeting_decisions", r["document"], r["record"])] = {"document": r["document"], "decision": norm}
        elif r["kind"] == "meeting" and r["field"] == "action":
            a = norm or {}
            recs[("fact_meeting_actions", r["document"], r["record"])] = {
                "document": r["document"], "owner": a.get("owner"), "task": a.get("task"), "due_date": a.get("due_date")}
        elif r["kind"] in ("contract", "invoice") and r["field"] != "parties":
            table = "fact_contracts" if r["kind"] == "contract" else "fact_invoices"
            rec = recs.setdefault((table, r["document"], ""), {"document": r["document"]})
            rec[r["field"]] = norm
            if r["id"] in lk:                            # the matched database row (joins with that table)
                rec[f"{r['field']}_key"] = lk[r["id"]]["row_key"]
    out: dict[str, list[dict]] = {t: [] for t in TABLES}
    for (table, doc, _), rec in recs.items():
        if table == "fact_contracts":
            rec["title"] = (cards.get(doc) or {}).get("title", doc)
        if table.startswith("fact_meeting"):
            rec["meeting_date"] = meet_date.get(doc)
        out[table].append({c: rec.get(c) for c in TABLES[table][1]})
    return out


def write_tables() -> bool:
    """Write the fact tables (Parquet) and their row access rules; True when a table changed."""
    import pandas as pd
    from rag.registry import access_map
    from semantic import access
    from semantic.config import DATA_DIR
    changed = False
    groups = access_map()
    rules = []
    for table, rows in approved_rows().items():
        path = DATA_DIR / f"{table}.parquet"
        if not rows:
            if path.exists():
                path.unlink()
                changed = True
            continue
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame(rows, columns=TABLES[table][1])
        for col in ("effective_date", "invoice_date", "meeting_date", "due_date"):
            if col in df:
                df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
        new = df.to_json(orient="records", date_format="iso")
        old = pd.read_parquet(path).to_json(orient="records", date_format="iso") if path.exists() else None
        if new != old:
            df.to_parquet(path, index=False)
            changed = True
        by_group: dict[str, list] = defaultdict(list)
        for doc in df["document"].unique():
            for g in groups.get(doc, ["public"]):
                by_group[g].append(doc)
        rules += [{"entity": table, "column": "document", "group": g, "values": sorted(docs)} for g, docs in by_group.items()]
    access.replace_rules(rules, source="facts")
    return changed


# ------------------------------------------------------------------ reading (agents)

def doc_card(name: str, groups: list[str] | None) -> dict | None:
    core = _db()
    r = core.db().execute("SELECT card FROM doc_cards WHERE tenant_id = ? AND name = ?", (core.current_tenant(), name)).fetchone()
    if not r:
        return None
    c = r["card"] if isinstance(r["card"], dict) else json.loads(r["card"])
    if groups is not None and not set(c.get("groups") or ["public"]) & set(groups):
        return None
    return {k: v for k, v in c.items() if k != "groups"}


def cards_for(names: list[str], groups: list[str] | None) -> list[dict]:
    out = []
    for n in dict.fromkeys(names):
        c = doc_card(n, groups)
        if c:
            out.append(c)
    return out


def facts_for(groups: list[str] | None, documents: list[str] | None = None, status: str = "approved") -> list[dict]:
    """Facts the caller may see (their documents' groups), approved only by default."""
    from rag.registry import access_map
    core = _db()
    amap = access_map()
    rows = core.db().execute("SELECT * FROM facts WHERE tenant_id = ? AND status = ?", (core.current_tenant(), status)).fetchall()
    out = []
    for r in rows:
        if documents and r["document"] not in documents:
            continue
        if groups is not None and not set(amap.get(r["document"], ["public"])) & set(groups):
            continue
        out.append({k: r[k] for k in ("id", "document", "kind", "record", "field", "value", "norm", "chunk_id", "quote",
                                       "confidence", "status")})
    return out
