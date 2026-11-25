import json, os, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
from rag.models import rerank_scores
chunks = json.loads((ROOT / "outputs/2_chunks.json").read_text())
texts = [c["text"] for c in chunks[100:120]]
for m in sys.argv[1:]:
    rerank_scores("warm up", texts[:2], m)
    t0 = time.perf_counter(); rerank_scores("How tall is Siniolchu?", texts, m)
    print(f"{m:<45} {(time.perf_counter()-t0)/len(texts)*1000:.0f} ms/passage", flush=True)
