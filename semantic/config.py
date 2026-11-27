"""Paths and settings for the semantic layer."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"                  # raw tables (CSV / Excel / Parquet), one file per table
REFERENCE_DIR = ROOT / "reference"        # trusted numbers (e.g. the finance team's report) for reconciliation
LAYER_DIR = ROOT / "semantic_layer"       # the REVIEWED, versioned layer: model, metrics, enrichment, instructions
STORE_DIR = ROOT / "sl_store"             # built enrichment store (DuckDB file); rebuilt from LAYER_DIR, not committed
OUT_DIR = ROOT / "outputs"                # one output file per step, like the RAG pipeline

MODEL_FILE = LAYER_DIR / "model.yaml"
METRICS_FILE = LAYER_DIR / "metrics.yaml"
ENRICHMENT_FILE = LAYER_DIR / "enrichment.yaml"
VERIFIED_FILE = LAYER_DIR / "verified_queries.yaml"
INSTRUCTIONS_FILE = LAYER_DIR / "instructions.md"
STORE_DB = STORE_DIR / "enrichment.duckdb"

TABLE_EXTS = {".csv", ".parquet", ".xlsx"}
SEMANTIC_COLLECTION = "semantic"          # Qdrant collection for metric/dimension/verified-question search
JOIN_MIN_CONTAINMENT = 0.9                # >= 90% of values found in a unique column -> suggested join
CATEGORY_MAX_DISTINCT = 50                # text columns with at most this many values are categories
VALUE_MATCH_MIN_SCORE = 80                # RapidFuzz score needed to accept a corrected filter value
