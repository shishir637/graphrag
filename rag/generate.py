"""Step 6 - Generation: Claude answers only from the retrieved passages and cites them
(structured output), saying "not found" instead of guessing.

Outputs: outputs/7_answer.json and an appended line in outputs/query_log.jsonl (simple observability)
"""
import json
import os
import time
from datetime import datetime, timezone

import anthropic
from pydantic import BaseModel, Field

from .config import CLAUDE_MODEL, OUT_DIR
from .retrieve import retrieve
from .utils import write_json

SYSTEM = """You answer questions using ONLY the numbered passages provided by the user.
- Base every statement on the passages; do not use outside knowledge.
- Cite the passage ids (e.g. "a1b2c3d4e5f6") that support the answer.
- If the passages do not contain the answer, set found=false and say briefly that the documents do not cover it.
- Keep the answer concise and direct."""


class Answer(BaseModel):
    found: bool = Field(description="True only if the passages contain the answer")
    answer: str
    citations: list[str] = Field(description="chunk_ids of the passages used")


_client = None


def workspace_headers() -> dict:
    """User-level keys (sk-ant-usr-...) must name a workspace; set ANTHROPIC_WORKSPACE_ID in .env."""
    ws = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    return {"anthropic-workspace-id": ws} if ws else {}


def claude() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(default_headers=workspace_headers())
    return _client


def format_passages(passages: list[dict]) -> str:
    parts = []
    for p in passages:
        where = f"{p['source']}" + (f", page {','.join(map(str, p['pages']))}" if p["pages"] else "")
        heading = " > ".join(p["headings"])
        parts.append(f"[{p['chunk_id']}] ({where}{' | ' + heading if heading else ''})\n{p['text']}")
    return "\n\n---\n\n".join(parts)


def can_answer() -> bool:
    """Claude (ANTHROPIC_API_KEY) or a local OpenAI-compatible model (GRAPH_LLM=openai, e.g. Ollama)."""
    from .config import GRAPH_LLM
    return bool(os.environ.get("ANTHROPIC_API_KEY")) or GRAPH_LLM == "openai"


def generate(question: str, passages: list[dict]) -> dict:
    user = f"<passages>\n{format_passages(passages)}\n</passages>\n\nQuestion: {question}"
    if not can_answer():
        return {"found": None, "answer": None, "citations": [], "model": None,
                "usage": {"input_tokens": 0, "output_tokens": 0}}
    if not os.environ.get("ANTHROPIC_API_KEY"):          # local model through the graph LLM client
        from .config import GRAPH_LLM_MODEL
        from .graph import llm
        parsed = llm.structured(SYSTEM, user, Answer)
        valid_ids = {p["chunk_id"] for p in passages}
        return {**parsed.model_dump(), "citations": [c for c in parsed.citations if c in valid_ids],
                "model": GRAPH_LLM_MODEL, "usage": {"input_tokens": 0, "output_tokens": 0}}
    resp = claude().beta.messages.parse(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",                    # if the model declines, the API retries on a fallback model
        system=SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_format=Answer,
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        parsed = Answer(found=False, answer=f"(no answer: stop_reason={resp.stop_reason})", citations=[])
    else:
        parsed = resp.parsed_output
    valid_ids = {p["chunk_id"] for p in passages}
    return {
        **parsed.model_dump(),
        "citations": [c for c in parsed.citations if c in valid_ids],
        "model": resp.model,
        "usage": {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens},
    }


def ask(question: str, groups=None, sources=None, save: bool = True) -> dict:
    t0 = time.perf_counter()
    ret = retrieve(question, groups, sources, save=save)
    t1 = time.perf_counter()
    gen = generate(question, ret["passages"])
    t2 = time.perf_counter()

    record = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "question": question,
        **gen,
        "contexts": [{"chunk_id": p["chunk_id"], "source": p["source"], "pages": p["pages"],
                      "rerank_score": p.get("rerank_score"), "text": p["text"]} for p in ret["passages"]],
        "timings_s": {**ret["timings_s"], "retrieval_total": round(t1 - t0, 3),
                      "generation": round(t2 - t1, 3), "total": round(t2 - t0, 3)},
    }
    if save:
        write_json("7_answer.json", record)
        with open(OUT_DIR / "query_log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record
