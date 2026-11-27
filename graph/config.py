"""Graph RAG settings."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
from rag.config import OUT_DIR  # noqa: E402 - per workspace
SCHEMA_FILE = ROOT / "graph_layer" / "schema.yaml"

# Apache AGE (PostgreSQL) - see docker-compose.graph.yml; local development defaults
PG_DSN = os.environ.get("GRAPH_PG_DSN", "host=127.0.0.1 port=5455 dbname=graphrag user=rag password=rag_local_dev")
GRAPH_NAME = "india_kg"            # AGE graph names need 3+ characters


def graph_name() -> str:
    """The workspace's AGE graph: india_kg in the first workspace, india_kg_<id> in others."""
    from rag.wspath import workspace_key
    k = workspace_key()
    return f"{GRAPH_NAME}_{k}"[:60] if k else GRAPH_NAME

# Models
EXTRACT_MODEL = "claude-haiku-5-5"      # cheap tier: entities + typed relations per chunk
SMALL_MODEL = "claude-haiku-5-5"        # entity-merge checks, community summaries, map step
ANSWER_MODEL = "claude-opus-5-5"        # final answers, reduce step

# Entity resolution
AUTO_MERGE_SCORE = 96                   # RapidFuzz token_sort_ratio at/above this: same entity
CANDIDATE_SCORE = 86                    # between this and AUTO: ask the LLM
CANDIDATE_COSINE = 0.90                 # BGE-M3 cosine of "name (type)" at/above this: ask the LLM
# Resolution by meaning (no LLM): an entity's context = the mean BGE-M3 vector of the passages that name it
CONTEXT_MERGE_COSINE = 0.80             # near-spellings (CANDIDATE..AUTO) merge when their contexts agree this much
ACRONYM_COSINE = 0.70                   # "SRE" and "Site Reliability Engineering" merge when contexts agree this much
CONTEXT_VETO_COSINE = 0.25              # an automatic spelling merge (< 99) is cancelled when contexts are this unlike

# Qdrant collections for the graph (chunks stay in "docs")
ENTITY_COLLECTION = "graph_entities"
COMMUNITY_COLLECTION = "graph_communities"

# Search
SEED_ENTITIES = 6                       # entities the question is matched to
MAX_RELATIONS = 40                      # relations put in the answer context
MAX_CHUNKS_RERANK = 8                   # source chunks reranked (reranker is ~3.5 s/passage on this CPU)
FINAL_CHUNKS = 5
GLOBAL_COMMUNITIES = 8                  # communities read for a big-picture question
