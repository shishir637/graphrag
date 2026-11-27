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

    # unanswerable questions are scored on refusing ("not found"), not with RAGAS
    scored = [(i, it, r) for i, (it, r) in enumerate(zip(items, runs)) if it.get("type") != "unanswerable"]
    dataset = EvaluationDataset.from_list([{
        "user_input": it["question"],
        "retrieved_contexts": [c["text"] for c in r["contexts"]] or [""],
        "response": r["answer"],
        "reference": it["reference"],
    } for _, it, r in scored])

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

    scores_by_index = {i: {m: (None if row[m] != row[m] else round(float(row[m]), 3)) for m in metric_cols}
                       for (_, row), (i, _, _) in zip(df.iterrows(), scored)}
    stages = ("dense", "bm25", "rrf", "final")

    def gold_hits(it, r):
        """Per retrieval stage: were ALL gold chunks present? (None if the item has no gold chunks)"""
        gold = set(it.get("gold_chunk_ids") or [])
        return {s: gold <= set(r["stage_ids"][s]) for s in stages} if gold and "stage_ids" in r else None

    per_q = []
    for i, (it, r) in enumerate(zip(items, runs)):
        per_q.append({
            "question": it["question"], "type": it.get("type", "single"), "regions": it.get("regions", []),
            "reference": it["reference"], "answer": r["answer"], "found": r["found"],
            "scores": scores_by_index.get(i, {m: None for m in metric_cols}),
            "gold_in_stage": gold_hits(it, r),
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

    types = sorted({q["type"] for q in per_q})
    with_gold = [q for q in per_q if q["gold_in_stage"]]
    unans = [q for q in per_q if q["type"] == "unanswerable"]
    summary = {
        "n_questions": len(items),
        "quality": {m: avg([q["scores"][m] for q in per_q]) for m in metric_cols},
        "quality_by_type": {t: {"n": sum(q["type"] == t for q in per_q),
                                **{m: avg([q["scores"][m] for q in per_q if q["type"] == t]) for m in metric_cols}}
                            for t in types if t != "unanswerable"},
        "gold_chunk_hit_rate": {s: round(sum(q["gold_in_stage"][s] for q in with_gold) / len(with_gold), 3)
                                for s in stages} if with_gold else None,
        "unanswerable_correctly_refused": (f"{sum(not q['found'] for q in unans)}/{len(unans)}" if unans else None),
        "answerable_but_refused": sum(not q["found"] for q in per_q if q["type"] != "unanswerable"),
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
            "gold_chunk_hit_rate": "share of questions whose answer chunk(s) survived each stage: dense/bm25 top-30, "
                                   "rrf top-20, final top-5 after rerank. A drop between two stages shows where "
                                   "the right passage is lost",
            "answer_relevancy": "low -> answer drifts from the question: prompt/generation",
        },
    }
    write_json("8_eval_results.json", {"summary": summary, "per_question": per_q})
    print(json.dumps({k: summary[k] for k in ("quality", "quality_by_type", "gold_chunk_hit_rate",
                                             "unanswerable_correctly_refused", "answerable_but_refused")}, indent=2))
    print(json.dumps(summary["efficiency"]["latency_s"]["total"], indent=2))


class _TypedQA(BaseModel):
    passage_ids: list[str]
    question: str
    reference: str


class _TypedQASet(BaseModel):
    items: list[_TypedQA]


def _regions_by_chunk(chunks: list[dict]) -> list[str | None]:
    """Region (state/UT) of each chunk, from the Heading-1 names in the build manifest.
    Chunks are in document order, so a chunk without a region heading inherits the previous one."""
    manifest = ROOT / "outputs" / "india_doc_manifest.json"
    names = set(json.loads(manifest.read_text(encoding="utf-8"))["regions"]) if manifest.exists() else set()
    out, current = [], None
    for c in chunks:
        hit = next((h for h in c["headings"] if h in names), None)
        if hit:
            current = hit
        out.append(current)
    return out


def _ask_for(kind: str, instructions: str, passages: list[tuple[str, str, dict]], n: int) -> list[dict]:
    block = "\n\n".join(f"<passage id='{pid}' region='{reg}' section='{' > '.join(c['headings'])}'>\n{c['text']}\n</passage>"
                        for pid, reg, c in passages)
    resp = claude().messages.parse(
        model=CLAUDE_MODEL, max_tokens=16000,
        messages=[{"role": "user", "content":
                   f"{block}\n\n{instructions}\nWrite exactly {n} items. Questions must name the state or union "
                   "territory they are about (a reader of a 1000-page book about India needs that to find the answer) "
                   "and must sound like a real user, not refer to 'the passage'. References are short, complete and "
                   "taken only from the passages. passage_ids lists the id(s) of the passage(s) holding the answer."}],
        output_format=_TypedQASet,
    )
    return [q.model_dump() | {"type": kind} for q in resp.parsed_output.items][:n]


def draft_mixed_questions(n: int = 60):
    """Eval set for a large multi-region document: questions spread across every region, in four types.
      single      - one fact from one passage, one question per region where possible
      multi_part  - two or three facts from the same passage
      comparison  - needs passages from two different regions
      unanswerable- about India but not covered by the document (should get "not found")
    Each item keeps the chunk ids holding the answer, so retrieval hit rate can be measured."""
    from . import passages
    chunks = passages.all()
    regions = _regions_by_chunk(chunks)
    usable = [(c, r) for c, r in zip(chunks, regions) if r and len(c["text"]) > 500 and "public" in c["access"]]
    by_region: dict[str, list[dict]] = {}
    for c, r in usable:
        by_region.setdefault(r, []).append(c)
    rng = random.Random(11)
    names = sorted(by_region)

    n_unans, n_compare, n_multi = round(n * 0.1), round(n * 0.15), round(n * 0.15)
    n_single = n - n_unans - n_compare - n_multi
    pick = lambda reg: rng.choice(by_region[reg])

    single_regions = (names * (n_single // len(names) + 1))[:n_single]
    rng.shuffle(single_regions)
    single = [(c["chunk_id"], r, c) for r in single_regions for c in [pick(r)]]

    multi_regions = rng.sample(names, n_multi)
    multi = [(c["chunk_id"], r, c) for r in multi_regions for c in [pick(r)]]

    topics = ["Economy", "Geography", "History", "Culture", "Demographics", "Education", "Transport", "Tourism"]
    compare = []
    while len(compare) < n_compare * 2:
        topic = rng.choice(topics)
        regs = [r for r in names if any(topic.lower() in h.lower() for c in by_region[r] for h in c["headings"])]
        if len(regs) < 2:
            continue
        for r in rng.sample(regs, 2):
            c = rng.choice([c for c in by_region[r] if any(topic.lower() in h.lower() for h in c["headings"])])
            compare.append((c["chunk_id"], r, c))

    items = []
    items += _ask_for("single", "Write one factual question per passage (a name, number, date, place or short "
                      "explanation).", single, n_single)
    items += _ask_for("multi_part", "Write one question per passage that asks for two or three related facts from that "
                      "passage at once.", multi, n_multi)
    items += _ask_for("comparison", "The passages come in consecutive pairs about the same topic in two different "
                      "regions. Write one question per pair that compares the two regions and needs both passages.",
                      compare, n_compare)

    outline = "\n".join(f"- {r}: " + ", ".join(sorted({h for c in by_region[r] for h in c['headings'][1:3]}))[:400]
                        for r in names)
    resp = claude().messages.parse(
        model=CLAUDE_MODEL, max_tokens=8000,
        messages=[{"role": "user", "content":
                   f"A document about India covers these regions and sections:\n{outline}\n\nWrite {n_unans} realistic "
                   "questions about Indian states that this document almost certainly does NOT answer (very specific "
                   "operational details such as a named official's phone number, today's bus timetable, a specific "
                   "shop's opening hours, a 2026 exam cutoff). reference must be exactly: \"The document does not "
                   "contain this information.\" passage_ids is an empty list."}],
        output_format=_TypedQASet,
    )
    items += [q.model_dump() | {"type": "unanswerable"} for q in resp.parsed_output.items][:n_unans]

    region_of = {c["chunk_id"]: r for c, r in zip(chunks, regions)}
    for it in items:
        it["gold_chunk_ids"] = it.pop("passage_ids")
        it["regions"] = sorted({region_of.get(cid) for cid in it["gold_chunk_ids"]} - {None})
    QUESTIONS_FILE.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    counts = {t: sum(i["type"] == t for i in items) for t in ("single", "multi_part", "comparison", "unanswerable")}
    print(f"  wrote {len(items)} questions to {QUESTIONS_FILE.name}: {counts} - review them before trusting the scores")


class _QA(BaseModel):
    question: str
    reference: str


class _QASet(BaseModel):
    items: list[_QA]


def draft_questions(n: int = 20):
    """Bootstrap an eval set: Claude writes question + reference answer pairs from random chunks.
    Review/edit eval_questions.json afterwards and add real user questions."""
    from . import passages
    chunks = [c for c in passages.all() if "public" in c["access"] and len(c["text"]) > 150]
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
