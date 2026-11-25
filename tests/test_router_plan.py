"""The router's evidence plan: a primary source plus cheap secondary ones, without a language model."""
from datetime import date

from app.router import plan

D = date(2026, 11, 25)


def test_why_question_about_a_metric_also_gets_the_number():
    primary, extra, _ = plan("Why did net revenue drop last quarter?", D)
    assert primary == "documents" and extra == ["data"]


def test_explain_a_metric_also_gets_the_passages():
    primary, extra, _ = plan("Explain how average order value is calculated", D)
    assert primary == "data" and extra == ["documents"]


def test_plain_questions_keep_one_source():
    assert plan("Net revenue by region last quarter", D)[:2] == ("data", [])
    assert plan("What is the leave policy?", D)[:2] == ("documents", [])
