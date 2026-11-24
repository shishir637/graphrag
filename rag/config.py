"""Central settings for the small RAG system. Change values here, not in the step modules."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = ROOT / "documents"          # drop your files here
OUT_DIR = ROOT / "outputs"             # every step writes one file here
QDRANT_PATH = ROOT / "qdrant_data"     # local, embedded Qdrant (no server needed)

# Files handled by Docling (layout-aware) vs. the plain-text splitter
DOCLING_EXTS = {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".md", ".png", ".jpg", ".jpeg", ".tiff"}
PLAINTEXT_EXTS = {".txt"}

# Embedding (dense) — BGE-M3, MIT licence, 1024-dim, 8K input
EMBED_MODEL = "BAAI/bge-m3"
EMBED_DIM = 1024
CHUNK_MAX_TOKENS = 512                 # sized with BGE-M3's own tokenizer

# Keyword search (sparse BM25, IDF computed by Qdrant)
SPARSE_MODEL = "Qdrant/bm25"

# Re-ranker
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"

# Retrieval sizes
COLLECTION = "docs"
CANDIDATES_PER_RETRIEVER = 30          # dense top-k and BM25 top-k before fusion
FUSED_TOP_K = 20                       # passages handed to the re-ranker
FINAL_TOP_K = 5                        # passages handed to Claude

# Generation / evaluation
CLAUDE_MODEL = "claude-opus-5-5"

# ---- GraphRAG (python rag.py graph-build / graph-ask / graph-eval) ----------------------------
import os as _os

# LLM used to extract entities/relationships and write summaries.
#   anthropic -> Claude via the same client as generation (default)
#   openai    -> any OpenAI-compatible server, e.g. Ollama/vLLM with an open model (fully open source)
GRAPH_LLM = _os.environ.get("GRAPH_LLM", "anthropic")
GRAPH_LLM_MODEL = _os.environ.get("GRAPH_LLM_MODEL", CLAUDE_MODEL)
GRAPH_LLM_BASE_URL = _os.environ.get("GRAPH_LLM_BASE_URL", "http://localhost:11434/v1")

# Schema the extractor may use. Keep it small and specific to your documents: a closed list
# gives cleaner graphs than letting the LLM invent types.
ENTITY_TYPES = ["Person", "Organization", "Project", "Product", "Technology", "Skill", "Location",
                "Role", "Document", "Metric", "Concept"]
RELATION_TYPES = ["WORKS_AT", "WORKS_ON", "HAS_ROLE", "HAS_SKILL", "USES", "PART_OF", "LOCATED_IN",
                  "OWNS", "DEPENDS_ON", "PRODUCES", "MEASURES", "RELATED_TO"]
EXTRACT_GLEANINGS = 1                  # extra "did you miss anything?" passes per chunk (0 = off)

# Entity resolution (rag/graph/resolve.py): exact -> name gate -> fuzzy -> embedding -> cluster -> LLM.
# Embedding thresholds are cosine similarity of the two NAMES' BGE-M3 vectors.
RESOLVE_EMBED_MATCH = 0.95             # >= match without asking anyone
RESOLVE_EMBED_NO_MATCH = 0.80          # <  clearly different; in between is ambiguous
RESOLVE_MAX_LLM_PAIRS = 50             # ambiguous pairs the LLM may judge per run (most similar first)
RESOLVE_USE_LLM = True                 # False -> ambiguous pairs stay "undecided"
# Your own decisions, applied before everything else: graph_resolution_overrides.json
#   {"match": [["Name A", "Name B"]], "no_match": [["Name A", "Name C"]]}

# Storage: Apache AGE (Postgres). "json" keeps everything in outputs/ and uses NetworkX instead.
GRAPH_STORE = _os.environ.get("GRAPH_STORE", "age")
GRAPH_DSN = _os.environ.get("GRAPH_DSN", "postgresql://clp:clp@127.0.0.1:5432/graphrag")
GRAPH_NAME = "doc_graph"

# Communities (Leiden) and their reports
LEIDEN_RESOLUTION = 1.0
MAX_COMMUNITY_SIZE = 25                # bigger communities are split again -> next level
COMMUNITY_MIN_SIZE = 2                 # singletons get no report

# Graph search
GRAPH_HOPS = 1                         # neighbourhood around the question's entities
GRAPH_CHUNKS = 15                      # chunks the graph contributes to hybrid fusion
ENTITY_LINK_MIN = 0.55                 # min cosine for question -> entity linking by embedding
GLOBAL_TOP_POINTS = 20                 # key points kept after the map step of global search
