"""Which facts may go live (no AI model).

1. Quote check: the fact's quote must be found in its passage (whitespace, quotes, dashes and digit-group commas
   normalised). If not, the fact is rejected: it would not be grounded in the source.
2. Constraints from the workspace's ontology (rules of kind "constraint", key "<kind>.<field>"): required fields,
   value ranges (notice 0-365 days...), amount > 0, at least two parties, field order (tax <= amount, an action is
   due on or after the meeting). A violation sends the fact (or the record, for a missing field) to needs_review.
3. Auto-approval: confidence >= 0.8, quote found and no violation; everything else waits in the owner's inbox.
Matching to database rows happens after storage (facts/match.py).
"""
from .extract import norm_text

AUTO_APPROVE = 0.8

DEFAULT_CONSTRAINTS = {
    "contract.parties": {"min_items": 2, "required": True},
    "contract.effective_date": {"required": True},
    "contract.notice_days": {"min": 0, "max": 365},
    "contract.term_months": {"min": 1, "max": 240},
    "contract.payment_terms_days": {"min": 0, "max": 180},
    "contract.annual_fee": {"min": 0.01},
    "invoice.invoice_number": {"required": True},
    "invoice.amount": {"min": 0.01, "required": True},
    "invoice.tax": {"min": 0, "lte": "amount"},
    "meeting.action": {"due_on_or_after": "meeting_date"},
}


def constraints() -> dict:
    """The workspace's approved constraints (seeded with the defaults the first time)."""
    try:
        from app import core
        from graph import ontology
        if not core.current_tenant():
            return DEFAULT_CONSTRAINTS
        if not core.get_setting("fact_constraints_seeded"):     # once; owners may change or retire them after
            ontology.import_schema({"constraints": DEFAULT_CONSTRAINTS}, source="template", by="facts")
            core.set_setting("fact_constraints_seeded", True)
        return ontology.load().get("constraints") or {}
    except ImportError:                                    # engines without the app
        return DEFAULT_CONSTRAINTS


def quote_ok(f: dict, chunks: dict) -> bool:
    c = chunks.get(f["chunk_id"])
    return bool(c) and bool(f["quote"].strip()) and norm_text(f["quote"]) in norm_text(c["text"])


def _violations(f: dict, rule: dict, record: dict) -> list[str]:
    v, out = f["norm"], []
    if "min_items" in rule and (not isinstance(v, list) or len(v) < rule["min_items"]):
        out.append(f"needs at least {rule['min_items']} values")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if "min" in rule and v < rule["min"]:
            out.append(f"below {rule['min']}")
        if "max" in rule and v > rule["max"]:
            out.append(f"above {rule['max']}")
        other = record.get(rule.get("lte"))
        if rule.get("lte") and isinstance(other, (int, float)) and v > other:
            out.append(f"greater than {rule['lte']}")
    if rule.get("due_on_or_after") and isinstance(v, dict) and v.get("due_date"):
        start = record.get(rule["due_on_or_after"])
        if start and v["due_date"] < start:
            out.append(f"due before the {rule['due_on_or_after'].replace('_', ' ')}")
    return out


def validate_document(document: str, kind: str, chunks: list[dict], facts: list[dict]) -> list[dict]:
    by_id = {c["chunk_id"]: c for c in chunks}
    rules = constraints()
    doc_record = {f["field"]: f["norm"] for f in facts if not f["record"]}
    for f in facts:
        problems = []
        if not quote_ok(f, by_id):
            f["status"], f["reason"] = "rejected", "the quote is not in the passage"
            continue
        problems += _violations(f, rules.get(f"{kind}.{f['field']}", {}), doc_record)
        if f["confidence"] < AUTO_APPROVE:
            problems.append(f"low confidence ({f['confidence']:.2f})")
        f["status"], f["reason"] = ("needs_review", "; ".join(problems)) if problems else ("approved", "")
    missing = [k.split(".", 1)[1] for k, r in rules.items()
               if k.startswith(kind + ".") and r.get("required") and k.split(".", 1)[1] not in doc_record]
    if missing and facts:
        for f in facts:
            if not f["record"] and f["status"] == "approved":
                f["status"], f["reason"] = "needs_review", "the document is missing: " + ", ".join(missing)
    return facts
