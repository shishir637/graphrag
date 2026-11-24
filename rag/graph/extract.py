"""g1 - Entity and relationship extraction, one chunk at a time. Three modes (config EXTRACT_MODE):

  hybrid (default) GLiNER2 reads every chunk on this machine. Its weakest chunks are ranked and,
                   up to ESCALATE_BUDGET of all chunks, sent to the LLM, which reviews GLiNER's
                   draft: keeps, corrects, drops or adds. Ranking (from agentic-graphrag):
                     1. two or more entities but no relationship found
                     2. lowest entity confidence first, for chunks whose weakest entity < ESCALATE_BELOW
                     3. chunks where nothing was found but that have >= ESCALATE_MIN_WORDS words
  gliner           GLiNER2 only. No LLM calls in this step.
  llm              the LLM reads every chunk (plus EXTRACT_GLEANINGS "anything missed?" passes).

Both extractors get the same closed list of entity and relation types with descriptions
(config.py), and the LLM returns structured output, so nothing is parsed out of free text. Results are cached by a hash of the chunk text and
the prompt, so re-running only pays for new or changed chunks. Every entity and relationship keeps
the chunk id it came from: that is the link back to the source text, to Qdrant and to access rules.

Output: outputs/g1_extraction.json
"""
import hashlib
import html
import json
import math

from pydantic import BaseModel, Field

from ..config import (ENTITY_DESCRIPTIONS, ENTITY_TYPES, ESCALATE_BELOW, ESCALATE_BUDGET, ESCALATE_MIN_WORDS,
                      EXTRACT_GLEANINGS, EXTRACT_MODE, GLINER_ENTITY_THRESHOLD, GLINER_MODEL,
                      GLINER_RELATION_THRESHOLD, RELATION_DESCRIPTIONS, RELATION_TYPES)
from .. import utils
from ..utils import read_json, write_json
from . import llm

PROMPT_VERSION = "v2"

SYSTEM = f"""You build a knowledge graph from document passages.
Extract the entities and the relationships between them that the passage states.

Entity types (use only these):
{chr(10).join(f"- {t}: {ENTITY_DESCRIPTIONS.get(t, '')}" for t in ENTITY_TYPES)}
Relationship types (use only these):
{chr(10).join(f"- {t}: {RELATION_DESCRIPTIONS.get(t, '')}" for t in RELATION_TYPES)}

Rules:
- Only what the passage says. No outside knowledge, no guesses.
- name: the most complete name used in the passage (e.g. "Priya R", not "she").
  Resolve pronouns inside the passage to the entity they refer to.
- description: one or two sentences about the entity, based on this passage only.
- A relationship's source and target must be names of entities you extracted.
- strength: 1-10, how explicitly and strongly the passage states the relationship.
- If nothing fits, return empty lists."""


class Entity(BaseModel):
    name: str
    type: str = Field(description="one of the allowed entity types")
    description: str


class Relation(BaseModel):
    source: str
    target: str
    type: str = Field(description="one of the allowed relationship types")
    description: str
    strength: int = Field(ge=1, le=10)


class Extraction(BaseModel):
    entities: list[Entity]
    relations: list[Relation]


def _key(chunk: dict) -> str:
    how = (f"{EXTRACT_MODE}|{EXTRACT_GLEANINGS}" if EXTRACT_MODE == "llm" else
           f"{EXTRACT_MODE}|{GLINER_MODEL}|{GLINER_ENTITY_THRESHOLD}|{GLINER_RELATION_THRESHOLD}")
    raw = (f"{PROMPT_VERSION}|{ENTITY_DESCRIPTIONS}|{RELATION_DESCRIPTIONS}|{how}|{chunk['embed_text']}")
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _user(chunk: dict, already: Extraction | None = None) -> str:
    context = " > ".join(chunk.get("headings") or [])
    attrs = f'source="{html.escape(chunk["source"])}" section="{html.escape(context)}"'
    msg = f"<passage {attrs}>\n{chunk['text']}\n</passage>"
    if already is not None:
        msg += ("\n\nYou already extracted:\n" + already.model_dump_json() +
                "\n\nMany entities and relationships are often missed on the first pass. "
                "Return ONLY the ones you missed (empty lists if none).")
    return msg


def _clean(ex: Extraction) -> tuple[list[dict], list[dict], list[str]]:
    """Drop anything outside the schema instead of trusting the LLM; report what was dropped."""
    problems, ents, seen = [], [], set()
    for e in ex.entities:
        name = " ".join(e.name.split())
        if not name:
            continue
        if e.type not in ENTITY_TYPES:
            problems.append(f"entity '{name}': type '{e.type}' not allowed -> Concept")
            e.type = "Concept"
        if name.lower() not in seen:
            seen.add(name.lower())
            ents.append({"name": name, "type": e.type, "description": e.description.strip()})
    names = {e["name"].lower() for e in ents}
    rels = []
    for r in ex.relations:
        s, t = " ".join(r.source.split()), " ".join(r.target.split())
        if s.lower() not in names or t.lower() not in names:
            problems.append(f"relation {s} -{r.type}-> {t}: endpoint not among extracted entities, dropped")
            continue
        if s.lower() == t.lower():
            continue
        if r.type not in RELATION_TYPES:
            problems.append(f"relation {s} -{r.type}-> {t}: type not allowed -> RELATED_TO")
            r.type = "RELATED_TO"
        rels.append({"source": s, "target": t, "type": r.type, "description": r.description.strip(),
                     "strength": r.strength})
    return ents, rels, problems


REVIEW = ("A fast local model has already read the passage and drafted the entities and relationships below, "
          "each with its confidence (0-1). Review the draft against the passage: keep what is correct, fix wrong "
          "names or types, drop what the passage does not support, and add what was missed. Return the complete "
          "corrected list, not only the changes.")


def _record(chunk, ents, rels, problems, extractor, escalation=None, dropped_local=()):
    return {"chunk_id": chunk["chunk_id"], "source": chunk["source"], "access": chunk["access"],
            "entities": ents, "relations": rels, "problems": problems + list(dropped_local),
            "extractor": extractor, "escalation": escalation}


def extract_chunk(chunk: dict) -> dict:
    """LLM only (mode llm)."""
    ex = llm.structured(SYSTEM, _user(chunk), Extraction)
    for _ in range(EXTRACT_GLEANINGS):
        more = llm.structured(SYSTEM, _user(chunk, ex), Extraction)
        if not more.entities and not more.relations:
            break
        ex = Extraction(entities=ex.entities + more.entities, relations=ex.relations + more.relations)
    ents, rels, problems = _clean(ex)
    for e in ents:
        e["from"] = "llm"
    return _record(chunk, ents, rels, problems, "llm")


def review_chunk(chunk: dict, local: dict, escalation: dict) -> dict:
    """The LLM corrects GLiNER's draft (mode hybrid, escalated chunks only)."""
    draft = {"entities": [{"name": e["name"], "type": e["type"], "confidence": e["confidence"]} for e in local["entities"]],
             "relations": [{"source": r["source"], "target": r["target"], "type": r["type"],
                            "confidence": r["confidence"]} for r in local["relations"]]}
    user = f"{_user(chunk)}\n\n<draft>\n{json.dumps(draft, ensure_ascii=False)}\n</draft>\n\n{REVIEW}"
    ents, rels, problems = _clean(llm.structured(SYSTEM, user, Extraction))
    local_conf = {e["name"].casefold(): e["confidence"] for e in local["entities"]}
    for e in ents:                                        # mark what the LLM kept vs added
        if e["name"].casefold() in local_conf:
            e["from"], e["confidence"] = "gliner+llm", local_conf[e["name"].casefold()]
        else:
            e["from"] = "llm"
    kept = {e["name"].casefold() for e in ents}
    removed = [f"LLM review dropped '{e['name']}' ({e['type']}, {e['confidence']:.2f})"
               for e in local["entities"] if e["name"].casefold() not in kept]
    return _record(chunk, ents, rels, problems + removed, "gliner+llm", escalation, local["dropped"])


def local_record(chunk: dict, local: dict, escalation: dict | None) -> dict:
    ents = [{"name": e["name"], "type": e["type"], "description": e["description"], "confidence": e["confidence"],
             "from": "gliner"} for e in local["entities"]]
    rels = [{k: r[k] for k in ("source", "target", "type", "description", "strength", "confidence")}
            for r in local["relations"]]
    return _record(chunk, ents, rels, [], "gliner", escalation, local["dropped"])


def escalation_plan(chunks: list[dict], local: dict[str, dict], total: int) -> tuple[list[str], dict]:
    """Which chunks the LLM reviews. Returns (selected chunk ids, per-chunk reason)."""
    eligible, why = [], {}
    for c in chunks:
        r = local[c["chunk_id"]]
        confs = [e["confidence"] for e in r["entities"]]
        weakest = min(confs) if confs else None
        if len(confs) >= 2 and not r["relations"]:
            rank, reason = (0, weakest or 0), f"{len(confs)} entities but no relationship found"
        elif weakest is not None and weakest < ESCALATE_BELOW:
            rank, reason = (1, weakest), f"weakest entity scored {weakest:.2f} (< {ESCALATE_BELOW})"
        elif not confs and len(c["text"].split()) >= ESCALATE_MIN_WORDS:
            rank, reason = (2, 0), f"nothing found in {len(c['text'].split())} words"
        else:
            continue
        eligible.append((rank, c["chunk_id"]))
        why[c["chunk_id"]] = {"reason": reason, "weakest": weakest}
    eligible.sort()
    limit = len(eligible) if ESCALATE_BUDGET >= 1 else min(len(eligible), math.ceil(ESCALATE_BUDGET * total)) \
        if ESCALATE_BUDGET > 0 else 0
    selected = [cid for _, cid in eligible[:limit]]
    for n, (_, cid) in enumerate(eligible):
        why[cid].update(rank=n + 1, escalated=n < limit)
    return selected, why


def extract_all() -> dict:
    if EXTRACT_MODE not in ("hybrid", "gliner", "llm"):
        raise SystemExit(f"EXTRACT_MODE must be hybrid, gliner or llm (got {EXTRACT_MODE!r})")
    if EXTRACT_MODE == "llm" and not llm.available():
        raise SystemExit("EXTRACT_MODE=llm needs an LLM: set ANTHROPIC_API_KEY, or GRAPH_LLM=openai for a local "
                         "model, or use EXTRACT_MODE=hybrid / gliner.")
    chunks = read_json("2_chunks.json")
    cache = {}
    if (utils.OUT_DIR / "g1_extraction.json").exists():        # same folder write_json uses
        cache = {r["cache_key"]: r for r in read_json("g1_extraction.json")["chunks"] if "cache_key" in r}

    calls_before = llm.usage["calls"]
    keys = {c["chunk_id"]: _key(c) for c in chunks}
    todo = [c for c in chunks if keys[c["chunk_id"]] not in cache]
    done: dict[str, dict] = {}
    plan_stats = None

    if EXTRACT_MODE == "llm":
        for i, c in enumerate(todo, 1):
            done[c["chunk_id"]] = extract_chunk(c)
            print(f"   llm {i}/{len(todo)} {c['chunk_id']}")
    else:
        from .gliner_extract import extract_local
        local = {}
        for i, c in enumerate(todo, 1):
            local[c["chunk_id"]] = extract_local(c["text"])
            if i % 20 == 0 or i == len(todo):
                print(f"   gliner {i}/{len(todo)} chunks")
        selected, why = (escalation_plan(todo, local, len(chunks)) if EXTRACT_MODE == "hybrid" else ([], {}))
        if selected and not llm.available():            # no LLM: weak chunks keep GLiNER's result
            print(f"   no LLM configured: {len(selected)} weak chunk(s) keep GLiNER's result unreviewed")
            for cid in selected:
                why[cid].update(escalated=False, reason=why[cid]["reason"] + " (no LLM configured)")
            selected = []
        for c in todo:
            if c["chunk_id"] not in selected:
                done[c["chunk_id"]] = local_record(c, local[c["chunk_id"]], why.get(c["chunk_id"]))
        for i, cid in enumerate(selected, 1):
            c = next(x for x in todo if x["chunk_id"] == cid)
            done[cid] = review_chunk(c, local[cid], why[cid])
            print(f"   llm review {i}/{len(selected)} {cid}: {why[cid]['reason']}")
        if EXTRACT_MODE == "hybrid":
            plan_stats = {"eligible": len(why), "escalated": len(selected), "budget": ESCALATE_BUDGET,
                          "llm_available": llm.available(),
                          "left_to_gliner": len(why) - len(selected),
                          "lowest_score_left": min((w["weakest"] for w in why.values()
                                                    if not w["escalated"] and w["weakest"] is not None), default=None)}

    out, reused = [], 0
    for c in chunks:
        if c["chunk_id"] in done:
            rec = {**done[c["chunk_id"]], "cache_key": keys[c["chunk_id"]]}
        else:
            rec, reused = {**cache[keys[c["chunk_id"]]], "chunk_id": c["chunk_id"], "access": c["access"]}, reused + 1
        out.append(rec)

    by = {}
    for r in out:
        by[r.get("extractor", "llm")] = by.get(r.get("extractor", "llm"), 0) + 1
    stats = {"mode": EXTRACT_MODE, "chunks": len(out), "reused_from_cache": reused,
             "llm_calls": llm.usage["calls"] - calls_before, "chunks_by_extractor": by,
             "escalation": plan_stats,
             "entity_mentions": sum(len(r["entities"]) for r in out),
             "relation_mentions": sum(len(r["relations"]) for r in out),
             "dropped_or_fixed": sum(len(r["problems"]) for r in out),
             "gliner_model": GLINER_MODEL if EXTRACT_MODE != "llm" else None,
             "schema": {"entity_types": ENTITY_TYPES, "relation_types": RELATION_TYPES}}
    write_json("g1_extraction.json", {"stats": stats, "chunks": out})
    return stats
