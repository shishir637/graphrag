"""Facts are checked before they go live: the quote must be in the passage, ontology constraints, and names are
matched to database rows (id in text, normalised names, RapidFuzz). Agents see approved facts only."""
import pytest

from app import core, jobs, vault
from facts.extract import extract
from facts.validate import validate_document
from tests.conftest import needs_pg
from tests.test_facts import INVOICE, MSA, chunk, write_chunks


def run_checks(kind, text):
    c = chunk(text)
    facts = [f.as_dict() for f in extract(kind, c)]
    return {f["field"]: f for f in validate_document("d", kind, [c], facts)}


def test_quote_check_rejects_ungrounded_facts():
    c = chunk(MSA)
    facts = [f.as_dict() for f in extract("contract", c)]
    facts[0]["quote"] = "between Acme and Somebody Else"
    out = validate_document("d", "contract", [c], facts)
    assert out[0]["status"] == "rejected" and "quote" in out[0]["reason"]
    assert all(f["status"] == "approved" for f in out[1:])
    # whitespace and digit-group commas do not matter
    facts = [f.as_dict() for f in extract("contract", c)]
    fee = next(f for f in facts if f["field"] == "annual_fee")
    fee["quote"] = fee["quote"].replace("12,00,000", "1200000").replace(" ", "  ")
    assert next(f for f in validate_document("d", "contract", [c], facts) if f["field"] == "annual_fee")["status"] == "approved"


def test_constraints_send_facts_to_review():
    f = run_checks("contract", MSA.replace("15 days' written notice", "400 days' written notice"))
    assert f["notice_days"]["status"] == "needs_review" and "above 365" in f["notice_days"]["reason"]
    f = run_checks("invoice", INVOICE.replace("GST 18%: INR 54,000", "GST 18%: INR 5,40,000"))
    assert f["tax"]["status"] == "needs_review" and "greater than amount" in f["tax"]["reason"]
    f = run_checks("contract", MSA.replace("This Agreement is made on 1 April 2026 between", "This Agreement is between"))
    assert "effective_date" not in f and f["notice_days"]["status"] == "needs_review"
    assert "missing: effective_date" in f["notice_days"]["reason"]


@pytest.fixture
def ws(tmp_path):
    jobs.stop_workers()
    core.reset_for_tests(tmp_path / "var")
    vault.reset_for_tests()
    return core.create_tenant("Acme")


@needs_pg
def test_names_are_matched_to_database_rows(ws):
    from facts import store
    from semantic import repo
    from semantic.config import DATA_DIR
    from semantic.schema import SemanticModel
    with core.tenant_scope(ws):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "vendor_master.csv").write_text(
            "vendor_id,legal_name,region\nV-4471,NORTHWIND TECHNOLOGIES LIMITED,South\nV-5120,KESTREL FACILITIES LLP,West\n"
            "V-7731,MERIDIAN DATA SERVICES PVT LTD,East\n")
        repo.import_layer(SemanticModel.model_validate({"entities": [{
            "name": "vendor_master", "table": "vendor_master", "primary_key": "vendor_id",
            "dimensions": [{"name": "legal_name", "column": "legal_name", "type": "text"},
                           {"name": "region", "column": "region", "type": "category"}]}]}),
            source="import", by="t", certified=True)
        sla = MSA.replace("Northwind Technologies Ltd", "Meridian Data Svcs Pvt Ltd")
        write_chunks({"MSA-001.txt": (MSA, ["public"]), "SLA-014.txt": (sla, ["public"]),
                      "INV-2101.txt": (INVOICE, ["public"])})
        store.run()
        rows = {(r["entity"], r["row_key"], r["status"]) for r in core.db().execute(
            "SELECT l.* FROM fact_links l JOIN facts f ON f.id = l.fact_id WHERE f.field IN ('party_2', 'vendor')")}
        assert ("vendor_master", "V-4471", "approved") in rows                      # suffixes ignored: exact match
        assert ("vendor_master", "V-7731", "needs_review") in rows                  # "Svcs": close, an owner checks
        from semantic.db import connect
        assert connect().execute("SELECT party_2_key FROM fact_contracts WHERE document = 'MSA-001.txt'").fetchone()[0] == "V-4471"
        got = connect().execute("SELECT v.region FROM fact_invoices i JOIN vendor_master v ON v.vendor_id = i.vendor_key").fetchone()
        assert got == ("South",)                                                   # a PDF invoice joined with the database


@needs_pg
def test_agents_only_see_approved_facts(ws):
    from facts import store
    with core.tenant_scope(ws):
        write_chunks({"MSA-001.txt": (MSA.replace("15 days'", "400 days'"), ["public"])})
        store.run()
        seen = {f["field"] for f in store.facts_for(["public"])}
        assert "notice_days" not in seen and "party_1" in seen
        assert {f["field"] for f in store.facts_for(None, status="needs_review")} == {"notice_days"}
        assert "notice_days" not in store.doc_card("MSA-001.txt", ["public"])["key_facts"]
