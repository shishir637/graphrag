#!/bin/bash
cd /home/claude/cl && source .venv/bin/activate
for ds in lumora india; do
 for m in Xenova/ms-marco-MiniLM-L-12-v2 jinaai/jina-reranker-v1-turbo-en BAAI/bge-reranker-v2-m3-int8 BAAI/bge-reranker-v2-m3; do
  tag=$(echo $m | sed 's|.*/||')
  echo "== $ds $m"
  CL_RERANKER=$m python - <<PY 2>&1 | grep -E "R@1|=="
import json, sys
sys.path.insert(0, '.')
from rag.retrieval_eval import run
if "$ds" == "lumora":
    run(["hybrid+rerank"], tag="rr_lumora_$tag", groups=["public","ops"], questions=json.load(open("experiments/lumora_eval_questions.json")))
else:
    run(["hybrid+rerank"], tag="rr_india_$tag", groups=["public"])
PY
 done
done
