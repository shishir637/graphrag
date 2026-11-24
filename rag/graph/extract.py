"""g1 - Entity and relationship extraction with an LLM, one chunk at a time.

The LLM gets a closed list of entity and relation types (config.py) and returns structured
output, so nothing is parsed out of free text. Results are cached by a hash of the chunk text and
the prompt, so re-running only pays for new or changed chunks. Every entity and relationship keeps
the chunk id it came from: that is the link back to the source text, to Qdrant and to access rules.

Output: outputs/g1_extraction.json
"""
import hashlib
import html
import json

from pydantic import BaseModel, Field

from ..config import ENTITY_TYPES, EXTRACT_GLEANINGS, OUT_DIR, RELATION_TYPES
from ..utils import read_json, write_json
from . import llm

PROMPT_VERSION = "v1"

SYSTEM = f"""You build a knowledge graph from document passages.
Extract the entities and the relationships between them that the passage states.

Entity types (use only these): {", ".join(ENTITY_TYPES)}
Relationship types (use only these): {", ".join(RELATION_TYPES)}

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
    raw = f"{PROMPT_VERSION}|{ENTITY_TYPES}|{RELATION_TYPES}|{EXTRACT_GLEANINGS}|{chunk['embed_text']}"
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


def extract_chunk(chunk: dict) -> dict:
    ex = llm.structured(SYSTEM, _user(chunk), Extraction)
    for _ in range(EXTRACT_GLEANINGS):
        more = llm.structured(SYSTEM, _user(chunk, ex), Extraction)
        if not more.entities and not more.relations:
            break
        ex = Extraction(entities=ex.entities + more.entities, relations=ex.relations + more.relations)
    ents, rels, problems = _clean(ex)
    return {"chunk_id": chunk["chunk_id"], "source": chunk["source"], "access": chunk["access"],
            "entities": ents, "relations": rels, "problems": problems}


def extract_all() -> dict:
    chunks = read_json("2_chunks.json")
    cache_path = OUT_DIR / "g1_extraction.json"
    cache = {}
    if cache_path.exists():
        cache = {r["cache_key"]: r for r in json.loads(cache_path.read_text(encoding="utf-8"))["chunks"]}

    calls_before = llm.usage["calls"]
    out, reused = [], 0
    for i, c in enumerate(chunks, 1):
        key = _key(c)
        if key in cache:
            rec, reused = {**cache[key], "chunk_id": c["chunk_id"], "access": c["access"]}, reused + 1
        else:
            rec = {**extract_chunk(c), "cache_key": key}
            print(f"   {i}/{len(chunks)} {c['chunk_id']}: {len(rec['entities'])} entities, "
                  f"{len(rec['relations'])} relations")
        out.append(rec)

    stats = {"chunks": len(out), "reused_from_cache": reused, "llm_calls": llm.usage["calls"] - calls_before,
             "entity_mentions": sum(len(r["entities"]) for r in out),
             "relation_mentions": sum(len(r["relations"]) for r in out),
             "dropped_or_fixed": sum(len(r["problems"]) for r in out),
             "schema": {"entity_types": ENTITY_TYPES, "relation_types": RELATION_TYPES}}
    write_json("g1_extraction.json", {"stats": stats, "chunks": out})
    return stats
