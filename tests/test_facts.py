"""Typed facts and document cards: document kinds, contract / invoice / meeting facts with provenance, fact tables
the semantic layer can query, row access per document group, and get_doc_card. No models."""
import json

import pytest

from app import core, vault
from facts.extract import extract, kind_of, parse_date
from tests.conftest import needs_pg

MSA = """MASTER SERVICES AGREEMENT MSA-001

1. Parties
This Agreement is made on 1 April 2026 between Acme Retail Pvt Ltd ("Client") and Northwind Technologies Ltd ("Vendor").

2. Services and Fees
The Vendor will provide IT support services to the Client for an annual fee of INR 12,00,000, payable quarterly. The Client shall pay each undisputed invoice within 30 days of receipt.

3. Term and Renewal
This Agreement runs for 12 months and renews automatically unless either party gives 15 days' written notice before expiry.

5. Liability
The Service Provider shall be liable for all losses arising from any breach of this Agreement.

7. Governing Law
This Agreement is governed by the laws of India, and the courts at Bengaluru have exclusive jurisdiction."""

INVOICE = """TAX INVOICE
Invoice No: INV-2101
Invoice Date: 3 October 2026
Bill From: Northwind Technologies Ltd
Bill To: Acme Retail Pvt Ltd
Amount (excl. GST): INR 3,00,000
GST 18%: INR 54,000"""

MEETING = """Vendor Review Meeting — 28 October 2026
Attendees: Priya (Legal), Meera (Finance), Arjun (Procurement)

Decisions
Decision: Renegotiate the Northwind notice period to 30 days before renewal.

Action items
Action: Priya to draft a non-renewal notice for Northwind by 10 November 2026.
Action: Arjun to schedule the Orion quarterly review by 20 November 2026."""


def chunk(text, cid="c1", source="doc.md"):
    return {"chunk_id": cid, "source": source, "access": ["public"], "headings": [], "text": text}


def by_field(facts):
    return {f.field: f for f in facts}


def test_kinds():
    assert kind_of("MSA-001.txt", MSA)[0] == "contract"
    assert kind_of("INV-2101.txt", INVOICE)[0] == "invoice"
    assert kind_of("review.md", MEETING)[0] == "meeting"
    assert kind_of("notes.md", "Lunch was nice. We talked about the weather.")[0] == "other"


def test_contract_facts_with_provenance():
    f = by_field(extract("contract", chunk(MSA)))
    assert f["party_1"].norm == "Acme Retail Pvt Ltd" and f["party_2"].norm == "Northwind Technologies Ltd"
    assert f["effective_date"].norm == "2026-04-01"
    assert f["term_months"].norm == 12 and f["notice_days"].norm == 15 and f["payment_terms_days"].norm == 30
    assert f["annual_fee"].norm == 1200000 and f["currency"].norm == "INR"
    assert f["governing_law"].norm == "India" and f["auto_renew"].norm is True and f["liability"].norm == "uncapped"
    for x in f.values():                                    # the quote is exactly the passage text at its span
        assert MSA[x.start:x.end] == x.quote and x.chunk_id == "c1"


def test_invoice_and_meeting_facts():
    f = by_field(extract("invoice", chunk(INVOICE)))
    assert f["invoice_number"].norm == "INV-2101" and f["invoice_date"].norm == "2026-10-03"
    assert f["vendor"].norm == "Northwind Technologies Ltd" and f["amount"].norm == 300000 and f["tax"].norm == 54000
    m = extract("meeting", chunk(MEETING))
    assert by_field(m)["meeting_date"].norm == "2026-10-28"
    assert by_field(m)["attendees"].norm == ["Priya", "Meera", "Arjun"]
    acts = [x.norm for x in m if x.field == "action"]
    assert acts[0] == {"owner": "Priya", "task": "draft a non-renewal notice for Northwind", "due_date": "2026-11-10"}
    assert [x.value for x in m if x.field == "decision"] == ["Renegotiate the Northwind notice period to 30 days before renewal."]


def test_dates():
    assert parse_date("1st April 2026") == "2026-04-01" and parse_date("April 1, 2026") == "2026-04-01"
    assert parse_date("31/12/2026") == "2026-12-31" and parse_date("2026-02-30") is None


@pytest.fixture
def ws(tmp_path):
    from app import jobs
    jobs.stop_workers()
    core.reset_for_tests(tmp_path / "var")
    vault.reset_for_tests()
    return core.create_tenant("Acme"), core.create_tenant("Globex")


def write_chunks(docs):
    from rag.config import OUT_DIR
    from rag import registry
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    chunks = []
    for name, (text, groups) in docs.items():
        registry.set_groups(name, groups)
        chunks.append(chunk(text, f"{name}-1", name))
    (OUT_DIR / "2_chunks.json").write_text(json.dumps(chunks), encoding="utf-8")


@needs_pg
def test_run_stores_facts_tables_cards_and_access(ws):
    from facts import store
    from semantic.access import rules
    from semantic.config import DATA_DIR
    a, b = ws
    with core.tenant_scope(a):
        write_chunks({"MSA-001.txt": (MSA, ["legal"]), "INV-2101.txt": (INVOICE, ["public"]),
                      "review.md": (MEETING, ["public"])})
        assert store.run() is True
        facts = store.facts_for(None)
        assert {f["kind"] for f in facts} == {"contract", "invoice", "meeting"}
        assert (DATA_DIR / "fact_contracts.parquet").exists() and (DATA_DIR / "fact_meeting_actions.parquet").exists()
        from semantic.db import connect
        row = connect().execute("SELECT party_2, notice_days, annual_fee FROM fact_contracts").fetchone()
        assert row == ("Northwind Technologies Ltd", 15, 1200000)
        assert {(r["entity"], r["group"]) for r in rules()} >= {("fact_contracts", "legal"), ("fact_invoices", "public")}
        assert store.run() is False                          # nothing changed: tables unchanged
        c = store.doc_card("MSA-001.txt", ["legal"])
        assert c["kind"] == "contract" and c["key_facts"]["notice_days"] == 15 and "Parties" not in c["title"]
        assert store.doc_card("MSA-001.txt", ["public"]) is None                 # not this caller's group
        assert store.doc_card("review.md", ["public"])["actions"][0]["owner"] == "Priya"
        assert not [f for f in store.facts_for(["public"]) if f["document"] == "MSA-001.txt"]
        # the data build drafts the fact tables like any other table
        from semantic.auto_model import draft
        from semantic.profile import profile_all
        profile_all()
        d = draft()
        assert "fact_contracts" in d["entities"]
    with core.tenant_scope(b):
        assert store.facts_for(None) == [] and store.doc_card("MSA-001.txt", None) is None
