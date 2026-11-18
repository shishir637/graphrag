"""Step 7 - Evaluation with RAGAS.

RAGAS scores QUALITY, separately per layer:
  retrieval  -> context_recall, context_precision   (did we find the right passages?)
  generation -> faithfulness, answer_relevancy       (is the answer grounded and on-topic?)
We also record EFFICIENCY ourselves: latency per stage and tokens per question.

Input : eval_questions.json  [{"question": ..., "reference": ...}, ...]
Output: outputs/8_eval_results.json  (per-question scores + averages + latency)
"""
import json
import random
import statistics

import anthropic
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import BaseModel

from .config import CLAUDE_MODEL, ROOT
from .generate import ask, claude, workspace_headers

_async_client = None


def claude_async() -> anthropic.AsyncAnthropic:
    global _async_client
    if _async_client is None:
        _async_client = anthropic.AsyncAnthropic(default_headers=workspace_headers(), max_retries=4)
    return _async_client
from .models import embed_dense
from .utils import read_json, write_json

QUESTIONS_FILE = ROOT / "eval_questions.json"


class _JudgeClaude(BaseChatModel):
    """Claude as the RAGAS judge, calling the Anthropic SDK directly.
    RAGAS asks for a temperature, but current Claude models reject sampling parameters,
    so this wrapper accepts and ignores them."""

    model: str = CLAUDE_MODEL

    @property
    def _llm_type(self) -> str:
        return "claude-judge"

    def _request(self, messages: list[BaseMessage]) -> dict:
        system = "\n\n".join(m.content for m in messages if m.type == "system")
        msgs = [{"role": "assistant" if m.type == "ai" else "user", "content": m.content}
                for m in messages if m.type != "system"]
        req = {"model": self.model, "max_tokens": 16000, "messages": msgs,
               "output_config": {"effort": "low"}}            # judging is a simple task
        if system:
            req["system"] = system
        return req

    @staticmethod
    def _result(resp) -> ChatResult:
        text = "".join(b.text for b in resp.content if b.type == "text")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return self._result(claude().messages.create(**self._request(messages)))

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return self._result(await claude_async().messages.create(**self._request(messages)))


class _BGEEmbeddings(Embeddings):
    """Reuses the pipeline's BGE-M3 model for RAGAS' answer-relevancy similarity."""

    def embed_documents(self, texts):
        return embed_dense(texts)

    def embed_query(self, text):
        return embed_dense([text])[0]


def run_eval():
    from ragas import EvaluationDataset, evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.run_config import RunConfig
    from ragas.metrics import (Faithfulness, LLMContextPrecisionWithReference, LLMContextRecall,
                               ResponseRelevancy)

    if not QUESTIONS_FILE.exists():
        raise SystemExit("eval_questions.json not found - run `python rag.py draft-questions` or write it by hand.")
    items = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))

    print(f"  answering {len(items)} questions...")
    runs = []
    for i, it in enumerate(items, 1):
        r = ask(it["question"], it.get("groups"), it.get("sources"), save=False)
        runs.append(r)
        print(f"   {i}/{len(items)} {r['timings_s']['total']}s  found={r['found']}")

    dataset = EvaluationDataset.from_list([{
        "user_input": it["question"],
        "retrieved_contexts": [c["text"] for c in r["contexts"]] or [""],
        "response": r["answer"],
        "reference": it["reference"],
    } for it, r in zip(items, runs)])

    llm = LangchainLLMWrapper(_JudgeClaude())
    emb = LangchainEmbeddingsWrapper(_BGEEmbeddings())
    print("  scoring with RAGAS (Claude as judge)...")
    result = evaluate(
        dataset,
        metrics=[LLMContextRecall(), LLMContextPrecisionWithReference(), Faithfulness(), ResponseRelevancy()],
        llm=llm, embeddings=emb, show_progress=True,
        # fewer parallel judge calls + longer per-job timeout: the default (16 workers, 180 s)
        # timed out on context precision, which makes one judge call per retrieved passage
        run_config=RunConfig(timeout=600, max_workers=4),
    )
    df = result.to_pandas()
    metric_cols = [c for c in df.columns if c not in ("user_input", "retrieved_contexts", "response", "reference")]

    per_q = []
    for (_, row), it, r in zip(df.iterrows(), items, runs):
        per_q.append({
            "question": it["question"], "reference": it["reference"], "answer": r["answer"], "found": r["found"],
            "scores": {m: (None if row[m] != row[m] else round(float(row[m]), 3)) for m in metric_cols},
            "timings_s": r["timings_s"], "usage": r["usage"],
            "retrieved": [f"{c['source']}#{c['chunk_id']}" for c in r["contexts"]],
        })

    def avg(vals):
        vals = [v for v in vals if v is not None]
        return round(statistics.mean(vals), 3) if vals else None

    def lat(key):
        vals = [r["timings_s"][key] for r in runs]
        return {"mean": round(statistics.mean(vals), 3), "p50": round(statistics.median(vals), 3),
                "max": round(max(vals), 3)}

    summary = {
        "n_questions": len(items),
        "quality": {m: avg([q["scores"][m] for q in per_q]) for m in metric_cols},
        "efficiency": {
            "latency_s": {k: lat(k) for k in ("retrieval_total", "rerank", "generation", "total")},
            "avg_input_tokens": avg([r["usage"]["input_tokens"] for r in runs]),
            "avg_output_tokens": avg([r["usage"]["output_tokens"] for r in runs]),
            "answered_rate": round(sum(r["found"] for r in runs) / len(runs), 3),
        },
        "how_to_read": {
            "context_recall": "low -> retrieval misses needed facts: tune chunking, hybrid weights, top-k",
            "llm_context_precision_with_reference": "low -> irrelevant passages ranked high: reranker/top-k",
            "faithfulness": "low -> answer not grounded in passages: prompt/generation",
            "answer_relevancy": "low -> answer drifts from the question: prompt/generation",
        },
    }
    write_json("8_eval_results.json", {"summary": summary, "per_question": per_q})
    print(json.dumps(summary["quality"], indent=2))
    print(json.dumps(summary["efficiency"]["latency_s"]["total"], indent=2))


class _QA(BaseModel):
    question: str
    reference: str


class _QASet(BaseModel):
    items: list[_QA]


def draft_questions(n: int = 20):
    """Bootstrap an eval set: Claude writes question + reference answer pairs from random chunks.
    Review/edit eval_questions.json afterwards and add real user questions."""
    chunks = [c for c in read_json("2_chunks.json") if "public" in c["access"] and len(c["text"]) > 150]
    random.seed(7)
    sample = random.sample(chunks, min(n, len(chunks)))
    per_chunk = max(1, n // len(sample))
    passages = "\n\n".join(f"<passage source='{c['source']}'>\n{c['text']}\n</passage>" for c in sample)
    resp = claude().messages.parse(
        model=CLAUDE_MODEL, max_tokens=16000,
        messages=[{"role": "user", "content":
                   f"{passages}\n\nWrite {per_chunk} question(s) per passage ({n} total) that a real user might ask, "
                   "each answerable from a single passage, plus a short reference answer taken from the passage. "
                   "Mix factual lookups (numbers, codes, names) with 'how/why' questions."}],
        output_format=_QASet,
    )
    items = [q.model_dump() for q in resp.parsed_output.items][:n]
    QUESTIONS_FILE.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  wrote {len(items)} draft questions to {QUESTIONS_FILE.name} - review them before trusting the scores")
