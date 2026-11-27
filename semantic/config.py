"""Paths and settings for the semantic layer."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
from rag.wspath import WorkspaceDir  # noqa: E402

DATA_DIR = WorkspaceDir("data", ROOT / "data")  # raw tables (CSV / Excel / Parquet), one file per table (per workspace)
REFERENCE_DIR = ROOT / "reference"        # trusted numbers (e.g. the finance team's report) for reconciliation
LAYER_DIR = ROOT / "semantic_layer"       # the REVIEWED, versioned layer: model, metrics, enrichment, instructions
OUT_DIR = WorkspaceDir("outputs", ROOT / "outputs")  # one output file per step, like the RAG pipeline

MODEL_FILE = LAYER_DIR / "model.yaml"
METRICS_FILE = LAYER_DIR / "metrics.yaml"
ENRICHMENT_FILE = LAYER_DIR / "enrichment.yaml"
VERIFIED_FILE = LAYER_DIR / "verified_queries.yaml"
INSTRUCTIONS_FILE = LAYER_DIR / "instructions.md"

TABLE_EXTS = {".csv", ".parquet", ".xlsx"}
SEMANTIC_COLLECTION = "semantic"          # Qdrant collection for metric/dimension/verified-question search
JOIN_MIN_CONTAINMENT = 0.9                # >= 90% of values found in a unique column -> suggested join
CATEGORY_MAX_DISTINCT = 50                # text columns with at most this many values are categories
VALUE_MATCH_MIN_SCORE = 80                # RapidFuzz score needed to accept a corrected filter value
