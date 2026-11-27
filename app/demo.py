"""Load the Kestrel Labs demo company as the company layer (Settings -> Demo company, owner only).

Replaces the company's shared data tables, documents and definitions with the Kestrel Labs set in demo/kestrel,
and fills the directory with its people and teams, and names the organisation Kestrel Labs. Personal layers, their tables and documents stay. Before
anything is removed, the old company definitions, tables and documents are saved in a zip under var/backups.
Every definition change is recorded in the history with the name of the person who loaded the demo."""
import json
import shutil
import time
import zipfile
from pathlib import Path

DEMO = Path(__file__).resolve().parent.parent / "demo" / "kestrel"


def available() -> bool:
    return (DEMO / "semantic_layer" / "metrics.yaml").exists()


def summary() -> dict:
    if not available():
        return {"available": False}
    return {"available": True, "company": "Kestrel Labs",
            "tables": sorted(p.stem for p in (DEMO / "data").glob("*.csv")),
            "documents": sorted(p.name for p in (DEMO / "documents").iterdir()),
            "people": max(0, sum(1 for _ in (DEMO / "directory.csv").open(encoding="utf-8")) - 1)}


def _company_tables() -> list[Path]:
    from semantic import config as SC
    from semantic.auto_model import table_layers
    personal = {t for t, layer in table_layers().items() if layer and layer != "company"}
    return [p for p in SC.DATA_DIR.iterdir() if p.is_file() and p.suffix.lower() in SC.TABLE_EXTS
            and not p.name.startswith("_") and p.stem not in personal] if SC.DATA_DIR.exists() else []


def _company_documents() -> list[str]:
    from app import engine
    return [d["name"] for d in engine.documents() if not any(g.startswith("layer:") for g in d["groups"])]


def backup() -> Path:
    from app import core, engine
    from semantic import config as SC
    out = core.DATA_DIR / "backups" / f"company-before-kestrel-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(SC.LAYER_DIR.glob("*")):
            if p.is_file():
                z.write(p, f"semantic_layer/{p.name}")
        for p in _company_tables():
            z.write(p, f"data/{p.name}")
        for name in _company_documents():
            z.write(engine.DOCS_DIR / name, f"documents/{name}")
    return out


def load(by: str):
    """The job: back up, replace company tables, documents and definitions, import the directory, rebuild."""
    from app import directory, engine
    from rag.config import OUT_DIR
    from semantic import config as SC, defstore
    from semantic.yamlio import _plain, raw_read
    if not available():
        raise RuntimeError("The demo files are missing from this installation (demo/kestrel).")
    print(f"[1/6] Saving the current company layer: {backup().name}")

    print("[2/6] Replacing the company data tables")
    for p in _company_tables():
        p.unlink()
    for p in (SC.OUT_DIR).glob("sl_1_profile.json"):
        p.unlink()
    SC.DATA_DIR.mkdir(parents=True, exist_ok=True)
    for p in sorted((DEMO / "data").glob("*.csv")):
        shutil.copy2(p, SC.DATA_DIR / p.name)

    print("[3/6] Replacing the company documents")
    for name in _company_documents():
        engine.delete_document(name)
    for p in sorted((DEMO / "documents").iterdir()):
        engine.save_document(p.name, p.read_bytes(), ["public"])

    print("[4/6] Replacing the company definitions (recorded in the history)")
    note = "Kestrel Labs demo company loaded"
    if defstore.enabled():
        with defstore.acting(by, "admin console", stack=(defstore.COMPANY,), write_layer=defstore.COMPANY):
            for name in defstore.SPEC:
                f = DEMO / "semantic_layer" / name
                if f.exists():
                    print(f"  {name}: {defstore.save_file(name, _plain(raw_read(f)) or {}, defstore.COMPANY, note)}")
    else:
        for name in ("model.yaml", "metrics.yaml", "enrichment.yaml", "verified_queries.yaml"):
            shutil.copy2(DEMO / "semantic_layer" / name, SC.LAYER_DIR / name)
    shutil.copy2(DEMO / "semantic_layer" / "instructions.md", SC.LAYER_DIR / "instructions.md")
    for f in ("eval_questions.yaml", "eval_questions_holdout.yaml"):     # the old company's test questions
        if (SC.LAYER_DIR / f).exists():
            (SC.LAYER_DIR / f).unlink()

    print("[5/6] Adding the people and teams to the directory")
    for t in json.loads((DEMO / "teams.json").read_text(encoding="utf-8")):
        directory.save_team(t["name"], t.get("lead_email"), t.get("parent"), t.get("description", ""))
    r = directory.import_csv((DEMO / "directory.csv").read_text(encoding="utf-8"), by)
    print(f"  directory: {r}")

    from app import core
    if core.get_setting("organisation", "") != "Kestrel Labs":
        print(f"  organisation name: {core.get_setting('organisation', '') or '(none)'} -> Kestrel Labs")
        core.set_setting("organisation", "Kestrel Labs")

    print("[6/6] Rebuilding search, metrics and the knowledge graph")
    engine.prepare_documents()
    engine.prepare_data()
    for p in OUT_DIR.glob("gr_extract_*.jsonl"):
        p.unlink()
    try:
        engine.build_graph()
    except Exception as e:                        # the graph is optional: search and metrics still work
        print(f"[graph] not built: {e}")
    print("Kestrel Labs is loaded.")


def start(by: str) -> dict:
    from app import jobs
    return jobs.enqueue("demo", "Loading the Kestrel Labs demo company", lambda info: load(info["by"]), by, {"by": by})
