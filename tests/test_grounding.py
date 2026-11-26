"""check_answer: every number, date, name and quote in a draft must be in the caller's evidence (passages by id
within their groups, approved facts); per-sentence verdicts with evidence ids. No AI model."""
import pytest

from app import core, jobs, vault
from app.grounding import check, claims, numbers
from tests.conftest import needs_pg
from tests.test_facts import MSA, write_chunks

pytestmark = needs_pg


def test_numbers_and_claims():
    assert 1200000 in numbers("INR 12,00,000") and 1200000 in numbers("12 lakh") and 1200000 in numbers("1.2 million")
    assert {c["type"] for c in claims('Asha Rao said "renew it" on 1 April 2026 for INR 5 lakh.')} == \
        {"name", "quote", "date", "number"}
    assert claims("It depends on the team.") == []


@pytest.fixture
def ws(tmp_path):
    jobs.stop_workers()
    core.reset_for_tests(tmp_path / "var")
    vault.reset_for_tests()
    t = core.create_tenant("Acme")
    with core.tenant_scope(t):
        write_chunks({"MSA-001.txt": (MSA, ["public"]), "secret.md": ("The settlement with Contoso was INR 9,00,000.", ["legal"])})
        from facts.store import run
        run(verbose=False)
    return t


def agent(groups):
    return {"id": "a1", "name": "bot", "groups": groups, "capabilities": ["documents"], "kind": "key"}


def test_supported_and_unsupported_sentences(ws):
    with core.tenant_scope(ws):
        draft = ("The agreement with Northwind Technologies Ltd started on 1 April 2026. "
                 "The annual fee is 12 lakh rupees, paid within 30 days. "
                 "Under the laws of India the notice period is 45 days. "
                 "It was signed by Ravi Menon. "
                 "Both sides want a good relationship.")
        r = check(agent(["public"]), "contract terms", draft, ["MSA-001.txt-1"])
        v = [s["verdict"] for s in r["sentences"]]
        assert v == ["supported", "supported", "partly_supported", "unsupported", "no_claims"]
        assert r["grounded"] is False and r["sentences"][0]["evidence"] == ["MSA-001.txt-1"] or \
            r["sentences"][0]["evidence"][0].startswith(("MSA", "fact:"))
        assert "45" in r["sentences"][2]["unsupported"][0] and r["sentences"][3]["unsupported"] == ["Ravi Menon"]
        ok = check(agent(["public"]), "fee", "The annual fee is INR 12,00,000.", ["MSA-001.txt-1"])
        assert ok["grounded"] is True


def test_evidence_stays_within_the_callers_groups(ws):
    with core.tenant_scope(ws):
        draft = "The settlement with Contoso was INR 9,00,000."
        assert check(agent(["public"]), "settlement", draft, ["secret.md-1"])["sentences"][0]["verdict"] == "unsupported"
        assert check(agent(["legal"]), "settlement", draft, ["secret.md-1"])["sentences"][0]["verdict"] == "supported"
