"""LLM (Claude) entity and relation extraction - same record format as graph/extract_trad.py.

One call per chunk with structured output: Claude reads the chunk together with its heading
path (so "the state" resolves to the region) and returns entities and relations restricted to
the types in graph_layer/schema.yaml, each with a one-line description (used later for
entity embeddings and community summaries - something traditional NER cannot produce).

  python -m graph.extract_llm [--chunks ids.json] [--out file.jsonl] [--workers 4]

Results are cached per chunk text hash in outputs/_graph_cache/claude/ so re-runs and new
documents only pay for chunks not seen before.
"""
import argparse
import hashlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from rag.config import CLAUDE_MODEL

MODEL = CLAUDE_MODEL          # set by --model; part of the cache key
from rag.llm import claude

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = yaml.safe_load((ROOT / "graph_layer" / "schema.yaml").read_text(encoding="utf-8"))
CHUNKS = ROOT / "outputs" / "2_chunks.json"
CACHE = ROOT / "outputs" / "_graph_cache" / "claude"
PROMPT_VERSION = "v1"

EntityType = Literal[tuple(SCHEMA["entity_types"])]
RelationType = Literal[tuple(SCHEMA["relation_types"])]


class _Entity(BaseModel):
    name: str = Field(description="canonical full name as written, e.g. 'Mlechchha dynasty', 'Hyderabad'")
    type: EntityType
    description: str = Field(description="one sentence about this entity, from the text only")


class _Relation(BaseModel):
    source: str = Field(description="entity name exactly as in entities")
    type: RelationType
    target: str = Field(description="entity name exactly as in entities")
    description: str = Field(description="one sentence stating the relation, from the text only")
    strength: int = Field(description="1-10: how explicit and important the relation is")


class _Extraction(BaseModel):
    entities: list[_Entity]
    relations: list[_Relation]


def _instructions() -> str:
    ents = "\n".join(f"- {k}: {v['description']}" for k, v in SCHEMA["entity_types"].items())
    rels = "\n".join(f"- {k}: {v['description']}" for k, v in SCHEMA["relation_types"].items())
    return (
        "Extract a knowledge graph from the passage.\n"
        f"Entity types:\n{ents}\n\nRelation types:\n{rels}\n\n"
        "Rules:\n"
        "- Only NAMED things (and crops/industries the passage names as a place's produce). No dates, numbers, "
        "generic nouns ('tourists', 'farmers') or species lists.\n"
        "- Use the heading path to resolve references: 'the state', 'its capital', 'the islands' refer to the "
        "region named in the headings. Add that region as an entity when the passage is about it.\n"
        "- One entity per real thing; use the fullest name in the passage ('Rohtang pass' not 'Rohtang').\n"
        "- Relations only between listed entities, only if the passage states or clearly implies them. "
        "Prefer specific types; use RELATED_TO only when none fits.\n"
        "- Enumerations like 'X Falls (Meghalaya)' or 'Y Sanctuary, Raigarh' are LOCATED_IN relations.")


def _key(chunk) -> str:
    return hashlib.sha1(f"{PROMPT_VERSION}|{MODEL}|{chunk['text']}|{chunk['headings']}".encode()).hexdigest()[:16]


def extract(chunk) -> dict:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{_key(chunk)}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8")) | {"cached": True}
    t = time.perf_counter()
    resp = claude().messages.parse(
        model=MODEL, max_tokens=8000, system=_instructions(),
        messages=[{"role": "user", "content": f"<headings>{' > '.join(chunk['headings'])}</headings>\n"
                                              f"<passage>\n{chunk['text']}\n</passage>"}],
        output_format=_Extraction,
    )
    out = resp.parsed_output
    names = {e.name for e in out.entities}
    rec = {
        "chunk_id": chunk["chunk_id"], "source": chunk["source"], "method": f"claude:{MODEL}",
        "entities": [e.model_dump() for e in out.entities],
        "relations": [r.model_dump() | {"strength": min(10, max(1, r.strength)), "rule": "llm"} for r in out.relations
                      if r.source in names and r.target in names and r.source != r.target],
        "dropped_relations": sum(1 for r in out.relations if r.source not in names or r.target not in names),
        "seconds": round(time.perf_counter() - t, 3),
        "usage": {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens},
    }
    f.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", help="JSON list of chunk ids (default: all chunks)")
    ap.add_argument("--out", help="output .jsonl (default outputs/gr_extract_claude.jsonl)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--model", default=CLAUDE_MODEL)
    ap.add_argument("--regions", help="comma-separated region names (Heading-1) to restrict to")
    a = ap.parse_args()
    global MODEL
    MODEL = a.model
    chunks = json.loads(CHUNKS.read_text(encoding="utf-8"))
    if a.chunks:
        wanted = set(json.loads(Path(a.chunks).read_text(encoding="utf-8")))
        chunks = [c for c in chunks if c["chunk_id"] in wanted]
    if a.regions:
        regs = {r.strip() for r in a.regions.split(",")}
        chunks = [c for c in chunks if regs & set(c["headings"])]
    out = Path(a.out) if a.out else ROOT / "outputs" / "gr_extract_claude.jsonl"
    t0, done = time.perf_counter(), 0
    with ThreadPoolExecutor(a.workers) as pool, out.open("w", encoding="utf-8") as f:
        for rec in pool.map(extract, chunks):
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            done += 1
            if done % 10 == 0 or done == len(chunks):
                print(f"    {done}/{len(chunks)} ({time.perf_counter() - t0:.0f}s)", flush=True)
    recs = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    fresh = [r for r in recs if not r.get("cached")]
    if fresh:
        tin = sum(r["usage"]["input_tokens"] for r in fresh) / len(fresh)
        tout = sum(r["usage"]["output_tokens"] for r in fresh) / len(fresh)
        print(f"  per chunk: {tin:.0f} input / {tout:.0f} output tokens, "
              f"{sum(r['seconds'] for r in fresh) / len(fresh):.1f}s per call")
    print(f"  -> {out}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
