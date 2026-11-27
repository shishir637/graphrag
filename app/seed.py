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


def land_sample_tables(by: str) -> int:
    """The shipped sample tables (./data) landed in the current workspace as Parquet, with manifest rows."""
    from app import landing
    from semantic.config import TABLE_EXTS
    src = ROOT / "data"
    if not src.exists():
        return 0
    n = 0
    for p in sorted(src.iterdir()):
        if not p.is_file() or p.name.startswith(".") or p.suffix.lower() not in TABLE_EXTS:
            continue
        if p.suffix.lower() == ".csv":
            landing.land_table_csv(p.stem, p.read_bytes(), source="sample", pushed_by=by)
        elif p.suffix.lower() == ".parquet":
            from app.storage import store
            tmp = store().scratch(".parquet")
            shutil.copyfile(p, tmp)
            landing.land_table_file(p.stem, tmp, source="sample", pushed_by=by)
        else:
            continue
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
    from semantic.config import LAYER_DIR
    if (os.environ.get("CL_SEED_LAYER", "1") == "0" and not force) or not (LAYER_DIR / "model.yaml").exists():
        return seed_new_workspace(by)
    from graph.config import SCHEMA_FILE
    from graph.ontology import seed_file, seed_generic
    onto = seed_file(by) if SCHEMA_FILE.exists() else seed_generic(by)
    tables = land_sample_tables(by)
    layer = repo.import_dir(LAYER_DIR, source="import", by=by, certified=True)
    if tables:                                    # known values (cities, channels...) for questions and agents
        from semantic.enrich import build_enrichment
        build_enrichment(embed=False)
    return {"tables": tables, "layer": layer, "ontology": onto}
