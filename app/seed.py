"""What a workspace starts with.

The server's first workspace (setup) gets the reviewed semantic layer in semantic_layer/ and the tables it
describes in data/, so an existing single-workspace installation keeps working. Workspaces made by sign-up start
empty. CL_SEED_LAYER=0 turns seeding off. python -m app.migrate_local also moves documents/ and the built outputs.
"""
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def copy_tree_files(src: Path, dst, exts: set[str] | None = None) -> int:
    """Copy the files of a folder (not recursive; skips names starting with '.')."""
    if not src.exists():
        return 0
    dst = Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for p in src.iterdir():
        if p.is_file() and not p.name.startswith(".") and (exts is None or p.suffix.lower() in exts):
            shutil.copy2(p, dst / p.name)
            n += 1
    return n


def seed_new_workspace(by: str) -> dict:
    """A workspace made by sign-up: the generic graph ontology (Person, Organisation, Place...)."""
    from graph.ontology import seed_generic
    seed_generic(by)
    return {"ontology": "generic"}


def seed_first_workspace(by: str, force: bool = False) -> dict:
    """Run inside the new workspace's scope. force: also when CL_SEED_LAYER=0 ("try sample data")."""
    from semantic import repo
    from semantic.config import DATA_DIR, LAYER_DIR, TABLE_EXTS
    if (os.environ.get("CL_SEED_LAYER", "1") == "0" and not force) or not (LAYER_DIR / "model.yaml").exists():
        return seed_new_workspace(by)
    from graph.config import SCHEMA_FILE
    from graph.ontology import seed_file, seed_generic
    onto = seed_file(by) if SCHEMA_FILE.exists() else seed_generic(by)
    tables = copy_tree_files(ROOT / "data", DATA_DIR.resolve_now(), set(TABLE_EXTS))
    layer = repo.import_dir(LAYER_DIR, source="import", by=by, certified=True)
    if tables:                                    # known values (cities, channels...) for questions and agents
        from semantic.enrich import build_enrichment
        build_enrichment(embed=False)
    return {"tables": tables, "layer": layer, "ontology": onto}
