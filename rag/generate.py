"""Step 6 - Generation: Claude answers only from the retrieved passages and cites them
(structured output), saying "not found" instead of guessing.

Outputs: outputs/7_answer.json and an appended line in outputs/query_log.jsonl (simple observability)
"""
import json
import time
from datetime import datetime, timezone

import anthropic
from pydantic import BaseModel, Field

from .config import CLAUDE_MODEL, OUT_DIR
from .llm import claude, workspace_headers  # noqa: F401  (re-exported)
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


def format_passages(passages: list[dict]) -> str:
    parts = []
    for p in passages:
        where = f"{p['source']}" + (f", page {','.join(map(str, p['pages']))}" if p["pages"] else "")
        heading = " > ".join(p["headings"])
        parts.append(f"[{p['chunk_id']}] ({where}{' | ' + heading if heading else ''})\n{p['text']}")
    return "\n\n---\n\n".join(parts)


def generate(question: str, passages: list[dict]) -> dict:
    user = f"<passages>\n{format_passages(passages)}\n</passages>\n\nQuestion: {question}"
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


def ask(question: str, groups=None, sources=None, save: bool = True, headings=None) -> dict:
    t0 = time.perf_counter()
    ret = retrieve(question, groups, sources, save=save, headings=headings)
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
        "stage_ids": ret["stage_ids"],
    }
    if save:
        write_json("7_answer.json", record)
        with open(OUT_DIR / "query_log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record
