"""Grounding check: sentences of an answer against cited passages and metric results (word-overlap mode)."""
import pytest

from app import grounding


@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    monkeypatch.setenv("CL_GROUNDING_RERANK", "0")


PASSAGES = [
    {"id": "c942f30d52f3", "text": "The root cause was that a retry in the renewal job sent the charge request "
                                   "twice when the payment provider answered slowly."},
    {"id": "bd7258a64630", "text": "Severity, = Sev1. Opened, = 2026-03-14 09:20 IST. Time to restore, = 5.5 hours. "
                                   "Incident owner, = Daniel Okafor"},
]
METRICS = grounding._flatten({"rows": [{"team_name": "Backend", "change_failure_rate": 0.1109989909},
                                       {"team_name": "Platform and DevOps", "cloud_cost": 225394.98}]})


def verdicts(answer):
    return {r["sentence"]: r["verdict"] for r in grounding.check(answer, PASSAGES, METRICS)["sentences"]}


def test_supported_sentences_including_rounded_numbers_and_dates():
    v = verdicts("A retry in the renewal job sent the charge request twice [c942f30d52f3]. "
                 "It was owned by Daniel Okafor and took 5.5 hours to restore. "
                 "Backend's change failure rate was 11.1%. Platform and DevOps spent about $225,395 on cloud. "
                 "It opened on 14 March 2026.")
    assert set(v.values()) == {"supported"}, v


def test_wrong_number_name_or_date_is_not_supported():
    r = grounding.check("It took 7 hours to restore. The incident was handled by Priya Jain. "
                        "It opened on 15 March 2026.", PASSAGES, METRICS)
    assert r["grounded"] is False and r["counts"]["not_supported"] == 3
    assert [x["not_in_evidence"] for x in r["sentences"]] == [["7"], ["Priya Jain"], ["15 March 2026"]]


def test_evidence_points_to_the_passage():
    r = grounding.check("The charge request was sent twice by a retry in the renewal job.", PASSAGES, [])
    assert r["sentences"][0]["passage_id"] == "c942f30d52f3" and r["grounded"]


def test_check_answer_needs_text():
    with pytest.raises(ValueError):
        grounding.check_answer("  ", ["public"])


def test_clock_times_and_iso_dates_are_not_numbers():
    r = grounding.check("The incident was caused by an expired certificate and took 9 hours to fix.", PASSAGES, [])
    s = r["sentences"][0]
    assert s["verdict"] == "not_supported" and s["not_in_evidence"] == ["9"] and not r["grounded"]
