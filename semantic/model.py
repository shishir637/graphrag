"""Phase 2 - Modelling: turn the profile into business terms.

The skeleton (entities, keys, dimensions, measures, joins) comes straight from the profile.
Claude drafts business labels and descriptions from column names and sample values.
The result is a DRAFT for a person to correct; the reviewed copy lives in semantic_layer/model.yaml.
networkx turns the joins into a graph so the compiler can find the path between any two tables.

Output: outputs/sl_2_model.draft.yaml  (+ semantic_layer/model.yaml if it does not exist yet)
"""
import shutil

import networkx as nx
from pydantic import BaseModel

from rag.utils import read_json

from .config import MODEL_FILE, OUT_DIR
from .schema import Dimension, Entity, Join, Measure, SemanticModel
from .yamlio import write

DRAFT_FILE = OUT_DIR / "sl_2_model.draft.yaml"
DIM_TYPE = {"category": "category", "time": "time", "text": "text", "key": "key"}


def skeleton(profile: dict) -> SemanticModel:
    entities = []
    for table, p in profile["tables"].items():
        dims, measures = [], []
        pk = p["primary_key_guess"] or next(iter(p["columns"]))
        for col, info in p["columns"].items():
            if info["role"] == "measure":
                measures.append(Measure(name=col, column=col, agg="sum"))
            elif info["semantic_type"] != "email":           # emails are not useful to group by
                dims.append(Dimension(name=col, column=col, type=DIM_TYPE[info["role"]]))
        measures.insert(0, Measure(name=f"{table}_count", column=pk, agg="count_distinct"))
        entities.append(Entity(name=table, table=table, primary_key=pk, dimensions=dims, measures=measures))
    joins = []
    for j in profile["suggested_joins"]:
        (fe, fc), (te, tc) = j["from"].split("."), j["to"].split(".")
        joins.append(Join(from_entity=fe, from_column=fc, to_entity=te, to_column=tc, relationship=j["relationship"]))
    return SemanticModel(entities=entities, joins=joins)


# --- LLM drafting of labels/descriptions ---
class _ColText(BaseModel):
    name: str
    label: str
    description: str


class _EntityText(BaseModel):
    entity: str
    label: str
    description: str
    fields: list[_ColText]


class _ModelText(BaseModel):
    entities: list[_EntityText]


def _profile_digest(profile: dict) -> str:
    lines = []
    for t, p in profile["tables"].items():
        lines.append(f"TABLE {t} ({p['rows']} rows)")
        for c, i in p["columns"].items():
            vals = ", ".join(v["value"] for v in i["top_values"][:5])
            lines.append(f"  {c}: {i['type']}, role={i['role']}, distinct={i['distinct']}, "
                         f"min={i['min']}, max={i['max']}, examples=[{vals}]")
    return "\n".join(lines)


def describe_with_llm(model: SemanticModel, profile: dict) -> SemanticModel:
    from rag.config import CLAUDE_MODEL
    from rag.llm import claude

    wanted = "\n".join(f"{e.name}: " + ", ".join([d.name for d in e.dimensions] + [m.name for m in e.measures])
                       for e in model.entities)
    resp = claude().messages.parse(
        model=CLAUDE_MODEL, max_tokens=16000,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content":
                   "You are drafting a semantic layer for business users. From this data profile, write a short "
                   "business label (2-4 words) and a one-sentence description for every entity and field listed. "
                   "Describe what the data means to the business, mention units or allowed values where the profile "
                   "shows them, and do not invent rules the profile does not show.\n\n"
                   f"<profile>\n{_profile_digest(profile)}\n</profile>\n\nEntities and fields:\n{wanted}"}],
        output_format=_ModelText,
    )
    text = {e.entity: e for e in resp.parsed_output.entities}
    for e in model.entities:
        t = text.get(e.name)
        if not t:
            continue
        e.label, e.description = t.label, t.description
        fields = {f.name: f for f in t.fields}
        for item in [*e.dimensions, *e.measures]:
            if item.name in fields:
                item.label, item.description = fields[item.name].label, fields[item.name].description
    return model


def draft_model(use_llm: bool = True) -> SemanticModel:
    profile = read_json("sl_1_profile.json")
    model = skeleton(profile)
    if use_llm:
        model = describe_with_llm(model, profile)
    write(DRAFT_FILE, model.model_dump(mode="json"),
          header="DRAFT semantic model generated from outputs/sl_1_profile.json.\n"
                 "Review it, fix names/descriptions/aggregations, then copy into semantic_layer/model.yaml via a pull request.")
    print(f"  -> wrote {DRAFT_FILE.relative_to(OUT_DIR.parent)}")
    if not MODEL_FILE.exists():
        shutil.copy(DRAFT_FILE, MODEL_FILE)
        print(f"  -> no reviewed model yet: copied the draft to {MODEL_FILE.relative_to(OUT_DIR.parent)}")
    return model


# --- join graph ---
def join_graph(model: SemanticModel) -> nx.DiGraph:
    """Edges point from the 'many' side to the 'one' side (orders -> customers -> regions).
    Following edges in that direction never duplicates rows, so metric totals stay correct."""
    g = nx.DiGraph()
    g.add_nodes_from(e.name for e in model.entities)
    for j in model.joins:
        g.add_edge(j.from_entity, j.to_entity, join=j)
        if j.relationship == "one_to_one":
            g.add_edge(j.to_entity, j.from_entity, join=j)
    return g


def join_path(model: SemanticModel, start: str, end: str) -> list[tuple[str, str, Join]]:
    """Steps (from_entity, to_entity, join) to walk from start to end."""
    g = join_graph(model)
    try:
        nodes = nx.shortest_path(g, start, end)
    except nx.NetworkXNoPath:
        raise ValueError(
            f"no safe join path from '{start}' to '{end}': it would need a one-to-many join, which duplicates "
            f"'{start}' rows and inflates totals. Use a metric defined on a more detailed entity instead.") from None
    return [(a, b, g.edges[a, b]["join"]) for a, b in zip(nodes, nodes[1:])]
