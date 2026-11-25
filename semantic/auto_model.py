"""Draft the semantic layer for new tables from the profile - no language model.

For every table in data/ that model.yaml does not describe yet:
  entity      one per table; primary key = the profile's unique key column (or the first column)
  dimensions  key, category, time, text and boolean columns, labelled from their names
  measures    numeric columns (sum and average), the row count
  joins       the profile's suggested many-to-one joins (>= 90% of values found in a unique column)
  metrics     DRAFT metrics: "<table> count" and "total <column>" / "average <column>" for numeric columns,
              each with the table's first date column as its time column

Drafts are written to model.yaml and metrics.yaml with status: draft. Agents only see approved metrics:
an admin reviews and approves them on the Metrics page (or edits the YAML), the same review the
hand-written definitions go through. Existing definitions are never changed.
"""
import re

from rag.utils import read_json

from .config import ENRICHMENT_FILE, METRICS_FILE, MODEL_FILE
from .yamlio import _plain, read, write

SCORE = re.compile(r"(csat|nps|score|rating|rate|ratio|pct|percent|age|year|month|day|lat|lon)", re.I)
MONEY = re.compile(r"(amount|price|revenue|cost|total|value|fee|salary|spend|paid|balance|budget|sales)", re.I)


def _label(col: str) -> str:
    return col.replace("__", " ").replace("_", " ").strip().capitalize()


def _dim_type(role: str, dtype: str) -> str:
    if role == "time":
        return "time"
    if role == "key":
        return "key"
    if dtype == "BOOLEAN":
        return "boolean"
    if role == "category":
        return "category"
    if role == "measure":
        return "number"
    return "text"


def draft(approve: bool = False) -> dict:
    prof = read_json("sl_1_profile.json")
    new_files = not MODEL_FILE.exists()
    model = read(MODEL_FILE) if MODEL_FILE.exists() else {"version": 1, "entities": [], "joins": []}
    metrics = read(METRICS_FILE) if METRICS_FILE.exists() else {"version": 1, "metrics": []}
    if "joins" not in model:
        model["joins"] = []
    have = {e["table"] for e in model["entities"]}
    words_before = {w for m in metrics["metrics"] for w in re.findall(r"[a-z]+", f"{m['label']} {m['name']}".lower())}
    names = {e["name"] for e in model["entities"]}
    metric_names = {m["name"] for m in metrics["metrics"]}
    added_entities, added_metrics = [], []
    status = "approved" if approve else "draft"
    new_tables = [t for t in prof["tables"] if t not in have]
    # a prefix every new table shares ("lumora_customers", "lumora_invoices") is left out of labels and synonyms
    prefix = ""
    if len(new_tables) >= 2:
        first = new_tables[0].split("_")[0] + "_"
        if all(t.startswith(first) for t in new_tables):
            prefix = first
    synonyms_out: list[tuple[str, str]] = []

    def plain(table_part: str) -> str:
        return table_part[len(prefix):] if prefix and table_part.startswith(prefix) else table_part

    for table, p in prof["tables"].items():
        if table in have:
            continue
        ent_name = re.sub(r"[^a-z0-9_]", "_", table.lower())
        if ent_name in names or not ent_name[0].isalpha():
            ent_name = "t_" + ent_name
        cols = p["columns"]
        pk = p.get("primary_key_guess") or next(iter(cols))
        dims, measures = [], [{"name": "row_count", "column": pk, "agg": "count", "label": f"{_label(table)} rows",
                               "description": f"Number of rows in {table}."}]
        time_col = None
        for col, info in cols.items():
            if not re.match(r"^[a-z][a-z0-9_]*$", col):
                continue
            t = _dim_type(info["role"], info["type"])
            if t == "time" and time_col is None:
                time_col = col
            if info["role"] == "measure":
                measures.append({"name": f"total_{col}", "column": col, "agg": "sum", "label": f"Total {_label(col).lower()}",
                                 "description": f"Sum of {col}."})
            dims.append({"name": col, "column": col, "type": t, "label": _label(col),
                         "description": f"{_label(col)} ({info['type'].lower()}, {info['distinct']} distinct values)."})
        ent = {"name": ent_name, "table": table, "label": _label(table),
               "description": f"One row per record of {table} ({p['rows']} rows). Drafted from the data profile; review.",
               "primary_key": pk, "dimensions": dims, "measures": measures}
        model["entities"].append(ent)
        names.add(ent_name)
        added_entities.append(ent_name)
        tc = f"{ent_name}.{time_col}" if time_col else None

        def add_metric(name, label, desc, expr, fmt):
            if name in metric_names:
                return
            m = {"name": name, "label": label, "description": desc, "type": "simple", "entity": ent_name,
                 "expression": expr, "format": fmt, "owner": "", "status": status}
            if tc:
                m["time_column"] = tc
            metrics["metrics"].append(m)
            metric_names.add(name)
            added_metrics.append(name)
        noun = plain(ent_name.split("__")[-1]).replace("_", " ")          # "tickets"
        unit_word = re.sub(r"s$", "", noun)                                  # "ticket"
        unit = re.sub(r"s$", "", ent_name.split("__")[-1])
        add_metric(f"{ent_name}_count", f"Number of {noun}", f"Count of distinct {pk} in {table}.",
                   f"count(DISTINCT {ent_name}.{pk})", "number")
        synonyms_out.append((noun, f"{ent_name}_count"))
        for col, info in cols.items():
            if info["role"] != "measure" or not re.match(r"^[a-z][a-z0-9_]*$", col):
                continue
            fmt = "currency" if MONEY.search(col) else "number"
            colw = _label(col).lower()
            if not SCORE.search(col):
                add_metric(f"total_{unit}_{col}", f"Total {unit_word} {colw}", f"Sum of {table}.{col}.",
                           f"sum({ent_name}.{col})", fmt)
                synonyms_out.append((f"{unit_word} {colw}", f"total_{unit}_{col}"))
            add_metric(f"average_{unit}_{col}", f"Average {unit_word} {colw}", f"Average of {table}.{col} per row.",
                       f"avg({ent_name}.{col})", fmt)
            if SCORE.search(col):
                synonyms_out.append((f"{unit_word} {colw}", f"average_{unit}_{col}"))
                synonyms_out.append((colw, f"average_{unit}_{col}"))
    # joins between drafted (or existing) entities
    table_to_ent = {e["table"]: e["name"] for e in model["entities"]}
    existing = {(j["from_entity"], j["from_column"], j["to_entity"]) for j in model.get("joins", [])}
    added_joins = []
    for j in prof.get("suggested_joins") or []:
        if j.get("relationship") != "many_to_one":
            continue
        (ft, fc), (tt, tcol) = j["from"].split(".", 1), j["to"].split(".", 1)
        if ft not in table_to_ent or tt not in table_to_ent:
            continue
        key = (table_to_ent[ft], fc, table_to_ent[tt])
        if key in existing or (ft in have and tt in have):
            continue
        model.setdefault("joins", []).append({"from_entity": key[0], "from_column": fc, "to_entity": key[2],
                                              "to_column": tcol, "relationship": "many_to_one"})
        existing.add(key)
        added_joins.append(f"{ft}.{fc} -> {tt}.{tcol}")
    if added_entities or added_joins:
        from .schema import MetricSet, SemanticModel
        SemanticModel.model_validate(_plain(model))
        MetricSet.model_validate(_plain(metrics))
        write(MODEL_FILE, model, "Semantic model (source of truth). Entities marked 'Drafted from the data profile'\n"
                                 "were written by semantic/auto_model.py and should be reviewed." if new_files else "")
        write(METRICS_FILE, metrics, "Metric definitions (source of truth). Agents see only status: approved."
              if new_files else "")
        en = read(ENRICHMENT_FILE) if ENRICHMENT_FILE.exists() else {"version": 1, "synonyms": [], "value_list_dimensions": []}
        vl = set(en.get("value_list_dimensions") or [])
        for e in model["entities"]:
            if e["name"] in added_entities:
                for d in e["dimensions"]:
                    if d["type"] == "category":
                        vl.add(f"{e['name']}.{d['name']}")
        en["value_list_dimensions"] = sorted(vl)
        # synonyms only where the word is not already taken by a metric, label or synonym
        taken = {x["term"].lower() for x in en.get("synonyms") or []}
        taken |= {str(m["label"]).lower() for m in metrics["metrics"]}
        for term, target in synonyms_out:
            clash = " " not in term and (term.lower() in words_before or re.sub(r"s$", "", term.lower()) in words_before)
            if term.lower() not in taken and not clash:
                en.setdefault("synonyms", []).append({"term": term, "target": target})
                taken.add(term.lower())
        write(ENRICHMENT_FILE, en, "" if ENRICHMENT_FILE.exists() else "Everyday words -> semantic layer names (reviewed).")
    return {"entities": added_entities, "metrics": added_metrics, "joins": added_joins}


def set_status(name: str, status: str, by: str = "") -> dict:
    """Approve or un-approve a metric. Approving records who reviewed it as the owner if none is set
    (every approved metric needs an owner)."""
    if status not in ("draft", "approved"):
        raise ValueError("status is draft or approved")
    metrics = read(METRICS_FILE)
    for m in metrics["metrics"]:
        if m["name"] == name:
            m["status"] = status
            if status == "approved" and not m.get("owner"):
                m["owner"] = by or "reviewer"
            break
    else:
        raise KeyError(name)
    from .schema import MetricSet
    MetricSet.model_validate(_plain(metrics))
    write(METRICS_FILE, metrics, "")
    return {"name": name, "status": status}
