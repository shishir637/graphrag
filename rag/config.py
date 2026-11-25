"""Central settings for the small RAG system. Change values here, not in the step modules."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
from .wspath import WorkspaceDir  # noqa: E402

# per workspace inside the app (rag/wspath.py); these folders on the command line
DOCS_DIR = WorkspaceDir("documents", ROOT / "documents")    # drop your files here
OUT_DIR = WorkspaceDir("outputs", ROOT / "outputs")         # every step writes one file here
QDRANT_PATH = ROOT / "qdrant_data"     # local, embedded Qdrant (no server needed)

# Files handled by Docling (layout-aware) vs. the plain-text splitter
DOCLING_EXTS = {".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".html", ".htm", ".md", ".png", ".jpg", ".jpeg", ".tiff"}
PLAINTEXT_EXTS = {".txt"}
RECORD_EXTS = {".json", ".jsonl", ".ndjson"}          # records rendered as "field: value" text, one block per record

# Embedding (dense) — BGE-M3, MIT licence, 1024-dim, 8K input
EMBED_MODEL = "BAAI/bge-m3"
EMBED_DIM = 1024
CHUNK_MAX_TOKENS = 512                 # sized with BGE-M3's own tokenizer

# Keyword search (sparse BM25, IDF computed by Qdrant)
SPARSE_MODEL = "Qdrant/bm25"

# Re-ranker (cross-encoder). CL_RERANKER overrides; "none" turns re-ranking off.
import os  # noqa: E402
RERANK_MODEL = os.environ.get("CL_RERANKER", "BAAI/bge-reranker-v2-m3-int8")
RERANK_MAX_CHARS = 1500                # longer passages are cut for the re-ranker only (speed; the agent gets the full text)

# Retrieval sizes
COLLECTION = "docs"
CANDIDATES_PER_RETRIEVER = 30          # dense top-k and BM25 top-k before fusion
FUSED_TOP_K = 20                       # passages handed to the re-ranker
FINAL_TOP_K = 5                        # passages handed to Claude

# Generation / evaluation
CLAUDE_MODEL = "claude-opus-5-5"
