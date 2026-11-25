"""The knowledge graph's ontology per workspace: seeded from graph_layer/schema.yaml, isolated between workspaces,
following the semantic model, connector templates, proposals from documents, re-extraction on change, and the
generic table mapping. No models needed."""
import json

import pytest
import yaml

from app import core, jobs, vault
from graph import ontology
from graph.config import SCHEMA_FILE
from tests.conftest import needs_pg, seed

pytestmark = needs_pg


@pytest.fixture
def ws(tmp_path):
    jobs.stop_workers()
    core.reset_for_tests(tmp_path / "var")
    vault.reset_for_tests()
    a, b = core.create_tenant("Acme"), core.create_tenant("Globex")
    seed(a)
    return a, b


def test_seeded_workspace_has_the_file_ontology_and_others_do_not_see_it(ws):
    from graph.nlp_extract import GENERIC_SCHEMA, schema
    a, b = ws
    f = yaml.safe_load(SCHEMA_FILE.read_text(encoding="utf-8"))
    with core.tenant_scope(a):
        s = schema()
        for k in ("spacy_label_map", "verb_rules", "copula_rules"):
            assert s[k] == f[k], k
        for k in ("entity_types", "relation_types"):              # the file's, plus those from the semantic model
            assert {n: s[k][n] for n in f[k]} == f[k], k
    with core.tenant_scope(b):
        assert ontology.rows() == []
        assert set(schema()["entity_types"]) == set(GENERIC_SCHEMA["entity_types"])   # generic fallback


def test_semantic_entities_and_joins_become_types(ws):
    a, _ = ws
    with core.tenant_scope(a):
        s = ontology.load()
        assert {"Customer", "Order", "Region", "Product"} <= set(s["entity_types"])
        r = s["relation_types"]["BELONGS_TO_REGION"]
        assert r["domain_type"] == "Customer" and r["range_type"] == "Region"
        assert ontology.get("entity_type", "Customer")["source"] == "semantic"


def test_documents_propose_types_for_approval_and_a_change_reextracts(ws, tmp_path):
    a, _ = ws
    rec = {"chunk_id": "c1", "entities": [{"name": f"{w} Policy", "type": "Document"} for w in
                                          ("Leave", "Travel", "Expense", "Security", "Privacy", "Hiring")]}
    f = tmp_path / "gr.jsonl"
    f.write_text(json.dumps(rec) + "\n")
    with core.tenant_scope(a):
        before = ontology.fingerprint()
        out = ontology.propose_from_extractions(str(f))
        assert [x["type"] for x in out] == ["Policy"]
        assert ontology.get("entity_type", "Policy")["status"] == "draft"
        assert "Policy" not in ontology.load()["entity_types"] and ontology.fingerprint() == before   # not used yet
        ontology.set_status("entity_type", "Policy", "approved", "owner@acme.io")
        s = ontology.load()
        assert "Policy" in s["entity_types"] and any(h["type"] == "Policy" for h in s["head_words"])
        assert ontology.fingerprint() != before                     # extraction records from before are redone
        assert [h["op"] for h in ontology.history("entity_type", "Policy")] == ["insert", "update"]


def test_github_connector_brings_its_ontology(ws, monkeypatch):
    from connectors import manage
    _, b = ws
    with core.tenant_scope(b):
        manage.create("github", "Code", {"repos": ["acme/orion"]}, {}, None, 60, "ann")
        s = ontology.load()
        assert {"Repository", "PullRequest", "Issue"} <= set(s["entity_types"])
        assert s["relation_types"]["FIXES"]["range_type"] == "Issue"
        assert ontology.get("entity_type", "Repository")["status"] == "approved"


def test_generic_table_mapping_from_the_semantic_model(ws):
    from graph.build import map_tables
    from semantic import repo
    from semantic.config import DATA_DIR
    from semantic.schema import SemanticModel
    _, b = ws
    with core.tenant_scope(b):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "teams.csv").write_text("team_id,team_name\nT1,Payments\nT2,Search\n")
        (DATA_DIR / "services.csv").write_text("service_id,service_name,team_id\nS1,Ledger API,T1\nS2,Indexer,T2\n"
                                               "S3,Refunds Worker,T1\n")
        (DATA_DIR / "deploys.csv").write_text("deploy_id,service_id,minutes\nD1,S1,4\nD2,S3,7\n")
        repo.import_layer(SemanticModel.model_validate({"entities": [
            {"name": "teams", "table": "teams", "primary_key": "team_id",
             "dimensions": [{"name": "team_name", "column": "team_name", "type": "category"}]},
            {"name": "services", "table": "services", "primary_key": "service_id",
             "dimensions": [{"name": "service_name", "column": "service_name", "type": "category"}]},
            {"name": "deploys", "table": "deploys", "primary_key": "deploy_id", "dimensions": []}],
            "joins": [{"from_entity": "services", "from_column": "team_id", "to_entity": "teams", "to_column": "team_id"},
                      {"from_entity": "deploys", "from_column": "service_id", "to_entity": "services",
                       "to_column": "service_id"}]}), source="import", by="t", certified=True)
        groups, rels = {}, []
        n = map_tables(groups, rels)
        assert n == 5                                              # 2 teams + 3 services; deploys are facts
        assert groups["ledger api"]["types"]["Service"] == 1 and groups["payments"]["types"]["Team"] == 1
        assert {(r["s"], r["t"], r["type"]) for r in rels} == {("ledger api", "payments", "BELONGS_TO_TEAM"),
                                                                ("indexer", "search", "BELONGS_TO_TEAM"),
                                                                ("refunds worker", "payments", "BELONGS_TO_TEAM")}


def test_yaml_export_import_roundtrip(ws):
    a, b = ws
    with core.tenant_scope(a):
        exported = ontology.export_schema()
    with core.tenant_scope(b):
        ontology.import_schema(exported, source="import", by="t")
        assert ontology.export_schema() == exported
