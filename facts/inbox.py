"""The owner's inbox: everything waiting for a person, in one list, with the quote and source to decide by.

  fact        a fact the checks were unsure about (low confidence, a constraint, a missing field)
  link        a name in a fact matched to a database row with a middling score
  definition  a drafted metric, synonym... or one a nightly check took out (needs_review)
  ontology    a proposed graph type
Decisions: approve, reject, or edit (facts: a corrected value; links: the right row key), each recorded with who
decided; facts and links decided by a person are kept as decided on every rebuild.

This module is the one place review items are listed (items, count) and decided (decide): the inbox page, the
Metrics and Ontology pages, and the MCP tools review_inbox, review_ontology and approve_metric all call it, and
the first sign-in guide counts with count(). An approved ontology change queues a graph re-extraction.
"""
import json

from .extract import parse_date, parse_number

NUMERIC = {"term_months", "notice_days", "payment_terms_days", "annual_fee", "amount", "tax"}
DATES = {"effective_date", "invoice_date", "meeting_date"}


def items(limit: int = 500) -> list[dict]:
    from app import core
    from graph import ontology
    from semantic import repo
    t = core.current_tenant()
    out = []
    for r in core.db().execute("SELECT * FROM facts WHERE tenant_id = ? AND status = 'needs_review' ORDER BY document, field "
                               "LIMIT ?", (t, limit)):
        out.append({"type": "fact", "id": r["id"], "title": f"{r['field'].replace('_', ' ')}: {r['value']}",
                    "document": r["document"], "kind": r["kind"], "quote": r["quote"], "passage": r["chunk_id"],
                    "reason": r["reason"], "confidence": r["confidence"], "value": r["value"]})
    for r in core.db().execute("SELECT l.*, f.value, f.document, f.quote, f.field FROM fact_links l JOIN facts f ON f.id = l.fact_id "
                               "WHERE l.tenant_id = ? AND l.status = 'needs_review' LIMIT ?", (t, limit)):
        out.append({"type": "link", "id": r["fact_id"], "title": f"{r['value']} = {r['label']} ({r['entity']} {r['row_key']})?",
                    "document": r["document"], "quote": r["quote"], "reason": f"match score {r['score']:.0f} ({r['method']})",
                    "score": r["score"], "value": r["row_key"]})
    for kind in repo.KINDS:
        for r in repo.rows(kind, ("draft", "needs_review")):
            out.append({"type": "definition", "id": f"{kind}:{r['name']}", "title": f"{kind} {r['name']}",
                        "reason": r["note"] or r["status"], "status": r["status"], "source": r["source"],
                        "definition": r["definition"]})
    for r in ontology.rows(None, ("draft",)):
        out.append({"type": "ontology", "id": f"{r['kind']}:{r['name']}", "title": f"{r['kind'].replace('_', ' ')} {r['name']}",
                    "reason": r["note"], "source": r["source"], "definition": r["definition"]})
    return out


def count() -> int:
    """How many items wait for a person (the same items as items())."""
    from app import core
    t = core.current_tenant()
    q = core.db().execute
    return (q("SELECT count(*) FROM sl_definitions WHERE tenant_id = ? AND status IN ('draft', 'needs_review')", (t,)).fetchone()[0]
            + q("SELECT count(*) FROM onto_definitions WHERE tenant_id = ? AND status = 'draft'", (t,)).fetchone()[0]
            + q("SELECT count(*) FROM facts WHERE tenant_id = ? AND status = 'needs_review'", (t,)).fetchone()[0]
            + q("SELECT count(*) FROM fact_links l JOIN facts f ON f.id = l.fact_id WHERE l.tenant_id = ? "
                "AND l.status = 'needs_review'", (t,)).fetchone()[0])


def ontology_changed(before: str, by: str):
    """An approved ontology change re-extracts the graph (only passages extracted with the old ontology)."""
    from app import engine, jobs
    from graph import ontology
    if ontology.fingerprint() != before and engine.graph_ready():
        jobs.enqueue("graph", "Building the knowledge graph", None, by, {"reason": "ontology changed"})


def _norm(field: str, value: str):
    if field in NUMERIC:
        n = parse_number(value.replace("INR", "").replace("₹", "").strip().split()[0])
        if n is None:
            raise ValueError(f"'{value}' is not a number.")
        return n
    if field in DATES:
        d = parse_date(value)
        if d is None:
            raise ValueError(f"'{value}' is not a date (e.g. 1 April 2026 or 2026-04-01).")
        return d
    return value


def decide(kind: str, item_id: str, action: str, by: str, value: str | None = None) -> dict:
    from app import core
    if action not in ("approve", "reject", "edit"):
        raise ValueError("Action is approve, reject or edit.")
    t = core.current_tenant()
    if kind == "fact":
        r = core.db().execute("SELECT * FROM facts WHERE tenant_id = ? AND id = ?", (t, item_id)).fetchone()
        if r is None:
            raise KeyError(item_id)
        if action == "edit":
            if not value:
                raise ValueError("Give the corrected value.")
            core.db().execute("UPDATE facts SET value = ?, norm = ?::jsonb, status = 'approved', decided_by = ?, "
                              "reason = 'edited by an owner', updated = ? WHERE tenant_id = ? AND id = ?",
                              (value, json.dumps(_norm(r["field"], value)), by, core.now(), t, item_id))
        else:
            core.db().execute("UPDATE facts SET status = ?, decided_by = ?, updated = ? WHERE tenant_id = ? AND id = ?",
                              ("approved" if action == "approve" else "rejected", by, core.now(), t, item_id))
        _refresh(by)
    elif kind == "link":
        r = core.db().execute("SELECT * FROM fact_links WHERE tenant_id = ? AND fact_id = ?", (t, item_id)).fetchone()
        if r is None:
            raise KeyError(item_id)
        st = "rejected" if action == "reject" else "approved"
        core.db().execute("UPDATE fact_links SET status = ?, row_key = ?, score = ?, decided_by = ?, updated = ? "
                          "WHERE tenant_id = ? AND fact_id = ?",
                          (st, value if action == "edit" and value else r["row_key"],
                           100.0 if action == "edit" else r["score"], by, core.now(), t, item_id))
        _refresh(by)
    elif kind == "definition":
        from semantic import repo
        k, _, name = item_id.partition(":")
        r = repo.get(k, name) if k in repo.KINDS else None
        if r is None or r["status"] == "retired":
            raise KeyError(item_id)
        repo.set_status(k, name, "approved" if action == "approve" else "retired", by)
    elif kind == "ontology":
        from graph import ontology
        k, _, name = item_id.partition(":")
        before = ontology.fingerprint()
        ontology.set_status(k, name, "approved" if action == "approve" else "retired", by)
        ontology_changed(before, by)
    else:
        raise ValueError("Type is fact, link, definition or ontology.")
    core.log_event("user" if "@" in by else "agent", by, "inbox.decide", f"{kind} {item_id}", {"action": action})
    return {"ok": True, "type": kind, "id": item_id, "action": action}


def _refresh(by: str):
    """Cards and fact tables follow the decision; the data build re-drafts changed fact tables in the background."""
    from facts.store import run
    if run(verbose=False):
        from app import jobs
        jobs.enqueue("data", "Preparing data tables", None, by, {})
