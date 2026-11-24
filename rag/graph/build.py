"""graph-build: run g1..g5 in order (each step reads the previous step's file in outputs/)."""
from ..config import EXTRACT_MODE, OUT_DIR
from . import llm


def build_graph(steps=("extract", "resolve", "communities", "summarize", "store")):
    from .communities import detect_all
    from .extract import extract_all
    from .resolve import resolve_all
    from .store import store_all
    from .summarize import summarize_all
    from .search import reset_cache
    funcs = {"extract": (f"[g1] Extracting entities and relationships ({EXTRACT_MODE})", extract_all),
             "resolve": ("[g2] Resolving entities", resolve_all),
             "communities": ("[g3] Detecting communities (Leiden)", detect_all),
             "summarize": ("[g4] Writing entity descriptions and community reports", summarize_all),
             "store": ("[g5] Storing the graph", store_all)}
    out = {}
    for s in steps:
        label, fn = funcs[s]
        print(label)
        out[s] = fn()
        print(f"     {out[s]}")
    reset_cache()
    (OUT_DIR / "g5_entity_vectors.json").unlink(missing_ok=True)     # re-embedded on the next question
    print(f"  graph LLM usage this run: {llm.usage}")
    return out
