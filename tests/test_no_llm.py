"""The parts that replaced the language model: rule planner, router, table ingestion, JSON documents,
graph search. No models, no AI key."""
import json
from datetime import date

import pytest


def test_rule_planner_gold_sets():
    from semantic.config import LAYER_DIR
    from semantic.rule_eval import score_set
    for name in ("eval_questions.yaml", "eval_questions_holdout.yaml"):
        r = score_set(LAYER_DIR / name)
        assert r["execution_accuracy"] >= 0.95, (name, r["failures"][:3])


@pytest.mark.parametrize("q,metric,dims", [
    ("Top 3 cities by net revenue in 2026", "net_revenue", ["customers.city"]),
    ("Net revenue by product category", "product_revenue", ["products.category"]),       # fan-out swap
    ("Monthly net revenue in 2026", "net_revenue", ["orders.order_date__month"]),
])
def test_rule_planner_cases(q, metric, dims):
    from semantic.rule_planner import planner
    p = planner().plan(q, date(2026, 11, 24))
    assert p.answerable and p.query.metrics == [metric] and p.query.dimensions == dims


def test_rule_planner_refuses():
    from semantic.rule_planner import planner
    for q in ("What is our profit margin?", "What was the marketing spend last quarter?"):
        assert not planner().plan(q, date(2026, 11, 24)).answerable


def test_router():
    from app.router import route
    assert route("Net revenue by region last quarter", date(2026, 11, 24))[0] == "data"
    assert route("Give me an overview of the main themes", date(2026, 11, 24))[0] == "overview"


def test_ingest_excel_and_json(tmp_path, monkeypatch):
    import pandas as pd
    from semantic import ingest
    monkeypatch.setattr(ingest, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ingest, "UPLOAD_DIR", tmp_path / "_uploads")
    xl = tmp_path / "src.xlsx"
    with pd.ExcelWriter(xl) as w:
        pd.DataFrame({"Employee ID": ["E1", "E2"], "Hire Date": ["2024-01-02", "2025-03-04"]}).to_excel(w, sheet_name="Staff", index=False)
        pd.DataFrame({"Dept Name": ["Ops"]}).to_excel(w, sheet_name="Depts", index=False)
    assert ingest.save_upload("people.xlsx", xl.read_bytes()) == ["people__staff", "people__depts"]
    df = pd.read_parquet(tmp_path / "people__staff.parquet")
    assert list(df.columns) == ["employee_id", "hire_date"] and str(df.hire_date[0]) == "2024-01-02"
    doc = {"exported": "x", "tickets": [{"id": 1, "customer": {"name": "A", "tier": "Gold"}, "tags": ["x", "y"]}]}
    assert ingest.save_upload("t.json", json.dumps(doc).encode()) == ["t"]
    df = pd.read_parquet(tmp_path / "t.parquet")
    assert set(df.columns) == {"id", "customer_name", "customer_tier", "tags"} and df.tags[0] == "x, y"
    assert ingest.save_upload("l.jsonl", b'{"a": 1}\n{"a": 2}\n') == ["l"]
    with pytest.raises(ValueError):
        ingest.save_upload("bad.json", b"[1, 2")


def test_json_documents_render_as_records(tmp_path):
    from rag.parse import records_to_text
    p = tmp_path / "incidents.json"
    p.write_text(json.dumps({"incidents": [{"id": "INC-1", "owner": {"name": "Jo"}, "tags": ["a", "b"]}]}))
    t = records_to_text(p)
    assert "incidents / incidents record 1" in t and "id: INC-1" in t and "owner.name: Jo" in t and "tags: a, b" in t


def test_graph_search_respects_groups(tmp_path, monkeypatch):
    from graph import search as GS
    build = {"entities": [
        {"id": "a", "name": "Grid Analytics", "type": "Organisation", "aliases": [], "chunk_ids": ["c1", "c2"]},
        {"id": "b", "name": "Meera Kulkarni", "type": "Person", "aliases": [], "chunk_ids": ["c1"]},
        {"id": "c", "name": "GridSense", "type": "Product", "aliases": [], "chunk_ids": ["c2", "c3"]}],
        "relations": [{"source_id": "a", "target_id": "b", "type": "RELATED_TO", "weight": 1, "chunk_ids": ["c1"],
                       "description": "Grid Analytics is headed by Meera Kulkarni."},
                      {"source_id": "c", "target_id": "a", "type": "RELATED_TO", "weight": 1, "chunk_ids": ["c3"],
                       "description": "GridSense is owned by Grid Analytics."}],
        "communities": []}
    f = tmp_path / "_graph_build.json"
    f.write_text(json.dumps(build))
    monkeypatch.setattr(GS, "BUILD", f)
    GS._G["mtime"] = None
    access = {"c1": ["public"], "c2": ["public"], "c3": ["finance"]}
    vis = lambda c, g: g is None or bool(set(access.get(c, [])) & set(g))            # noqa: E731
    r = GS.local("Who heads the department that owns GridSense?", ["public", "finance"], vis)
    assert {e["name"] for e in r["entities"]} == {"GridSense"} and "Meera Kulkarni" in {b["name"] for b in r["bridges"]} | {x["target"] for x in r["relations"]} | {x["source"] for x in r["relations"]}
    r = GS.local("Who heads the department that owns GridSense?", ["public"], vis)
    assert all("c3" not in x["passages"] for x in r["relations"])                    # finance-only link hidden
    e = GS.entity("grid analytics", ["public"], vis)
    assert e["name"] == "Grid Analytics" and all(l["entity"] != "GridSense" for l in e["links"])
