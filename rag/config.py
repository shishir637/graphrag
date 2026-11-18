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
