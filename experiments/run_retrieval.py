import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
from rag.retrieval_eval import run
tag = sys.argv[1]; which = sys.argv[2:] or ["india", "lumora"]
if "lumora" in which:
    qs = json.loads((ROOT / "experiments/lumora_eval_questions.json").read_text())
    print("== Lumora (groups public+ops)", flush=True)
    run(tag=f"lumora_{tag}", groups=["public", "ops"], questions=qs)
if "india" in which:
    print("== India document", flush=True)
    run(tag=f"india_{tag}", groups=["public"])
