"""Checks for the semantic-layer evaluator and the accuracy helpers it measures."""
from datetime import date

import pytest

from semantic.dates import resolve
from semantic.evaluate import check_gold, execution_match, number_faithfulness, request_match

T = date(2026, 11, 24)        # a Tuesday in fiscal Q3 of FY2026-27


def _one(q, today=T):
    hits = resolve(q, today)
    assert len(hits) == 1, hits
    return str(hits[0]["start_date"]), str(hits[0]["end_date"])


# phrasings that are NOT in the gold set, so the resolver is not just fitted to it
@pytest.mark.parametrize("q, want", [
    ("revenue for FY 24-25", ("2024-04-01", "2025-03-31")),
    ("orders in the previous fiscal year", ("2025-04-01", "2026-03-31")),
    ("AOV in Q4 of FY2025-26", ("2026-01-01", "2026-03-31")),
    ("units in Q1 2026", ("2026-01-01", "2026-03-31")),
    ("refunds during the previous quarter", ("2026-07-01", "2026-09-30")),
    ("signups in H2 2025", ("2025-07-01", "2025-12-31")),
    ("sales for March 2026", ("2026-03-01", "2026-03-31")),
    ("revenue in the past 30 days", ("2026-10-26", "2026-11-24")),
    ("orders since 15 August 2026", ("2026-08-15", "2026-11-24")),
])
def test_date_phrases(q, want):
    assert _one(q) == want


def test_last_quarter_in_january_is_previous_year_q4():
    assert _one("revenue last quarter", date(2026, 1, 10)) == ("2025-10-01", "2025-12-31")


def test_fiscal_year_in_march_belongs_to_previous_start_year():
    assert _one("revenue this fiscal year", date(2026, 3, 15)) == ("2025-04-01", "2026-03-31")


def test_no_dates_no_hints():
    assert resolve("net revenue by region", T) == []


def test_gold_set_compiles():
    assert check_gold() == []


def test_execution_match_ignores_order_and_extra_metrics_but_not_grouping():
    gold = [{"region": "North", "m": 10.0}, {"region": "South", "m": 5.0}]
    assert execution_match([{"region": "South", "m": 5.0, "x": 1}, {"region": "North", "m": 10.0, "x": 2}],
                           gold, ["m", "x"], ["m"])
    assert not execution_match([{"m": 15.0}], gold, ["m"], ["m"])                 # lost the grouping
    assert not execution_match([{"region": "North", "m": 9.0}, {"region": "South", "m": 5.0}], gold, ["m"], ["m"])


def test_request_match_parts():
    g = {"metrics": ["net_revenue"], "filters": [{"dimension": "customers.city", "op": "=", "values": ["Bengaluru"]}]}
    p = {"metrics": ["net_revenue"], "filters": [{"dimension": "customers.city", "op": "=", "values": ["bengaluru"]}],
         "start_date": "2026-07-01"}
    m = request_match(p, g)
    assert m["metrics"] and m["filters"] and not m["dates"] and not m["exact"]


def test_number_faithfulness_catches_invented_numbers():
    rows = [{"net_revenue": 1234567.4, "return_rate": 0.0821}]
    assert number_faithfulness("Net revenue was ₹1,234,567 and the return rate 8.2%.", rows, "q") == 1.0
    assert number_faithfulness("Net revenue was ₹1,500,000.", rows, "q") == 0.0
    assert number_faithfulness("Net revenue in 2025 was ₹1,234,567.", rows, "q") == 1.0    # the year is ignored
