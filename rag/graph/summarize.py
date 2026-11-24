"""g4 - Entity descriptions and community reports.

Entity description: an entity seen in one chunk keeps that chunk's description as-is (no LLM call);
an entity seen in several chunks gets one merged description written by the LLM.

Community report: title, summary, importance and key findings, written by the LLM from the
community's entities and relationships. Reports are what "global" questions are answered from.

Access: every summary records the chunks it was written from. A user may only be shown a
summary if they can see ALL of those chunks; otherwise search falls back to the raw per-chunk
descriptions they are allowed to see. That keeps summaries from leaking restricted text.

Output: outputs/g4_summaries.json
"""
import re
from collections import Counter

from pydantic import BaseModel, Field

from ..config import COMMUNITY_MIN_SIZE
from ..utils import read_json, write_json
from . import llm

MAX_REL_IN_REPORT = 60

_ENTITY_SYS = ("You merge several descriptions of the same entity, each taken from a different passage, "
               "into one description of at most four sentences. Keep every distinct fact, drop repetition, "
               "and when descriptions contradict each other say so. Use only the given descriptions.")

_REPORT_SYS = ("You write a report about one community of a knowledge graph built from an organisation's "
               "documents. Use only the entities and relationships given. Findings must be specific "
               "(names, numbers, roles) and each must be supported by the data given.")


class _Desc(BaseModel):
    description: str


class Finding(BaseModel):
    summary: str
    explanation: str


class Report(BaseModel):
    title: str = Field(description="short name naming the key entities")
    summary: str = Field(description="what this community is about, 2-4 sentences")
    importance: int = Field(ge=0, le=10, description="how central this community is to the collection")
    findings: list[Finding] = Field(description="3-6 key findings")


def _entity_summary(e: dict) -> tuple[str, bool]:
    distinct = list(dict.fromkeys(d["text"] for d in e["descriptions"] if d["text"]))
    if len(distinct) <= 1:
        return (distinct[0] if distinct else ""), False
    if not llm.available():                    # no LLM: the distinct sentences, repeats removed, at most four
        sents = list(dict.fromkeys(x.strip() for t in distinct for x in re.split(r"(?<=[.!?])\s+", t) if x.strip()))
        return " ".join(sents[:4]), False
    text = "\n".join(f"- {t}" for t in distinct)
    return llm.structured(_ENTITY_SYS, f"Entity: {e['name']} ({e['type']})\nDescriptions:\n{text}", _Desc).description, True


def template_report(c: dict, ents: dict, rels: list[dict], summaries: dict) -> Report:
    """A report written by code, used when no LLM is configured. Same fields as the LLM report."""
    deg = Counter()
    for r in rels:
        deg[r["source"]] += r["weight"]
        deg[r["target"]] += r["weight"]
    order = sorted(c["entity_ids"], key=lambda i: (-deg[i], ents[i]["name"]))
    by_type = {}
    for i in order:
        by_type.setdefault(ents[i]["type"], []).append(ents[i]["name"])
    parts = [f"{len(v)} {t}{'s' if len(v) > 1 else ''} ({', '.join(v[:4])}{', ...' if len(v) > 4 else ''})"
             for t, v in sorted(by_type.items(), key=lambda x: -len(x[1]))]
    phrase = lambda r: f"{ents[r['source']]['name']} {r['type'].lower().replace('_', ' ')} {ents[r['target']]['name']}"
    findings = [Finding(summary=phrase(r), explanation=r["descriptions"][0]["text"]) for r in rels[:5]]
    if not findings:
        findings = [Finding(summary=ents[i]["name"], explanation=summaries[i]["summary"]) for i in order[:3]]
    return Report(title=" / ".join(ents[i]["name"] for i in order[:3]),
                  summary=f"{len(order)} entities: {'; '.join(parts)}." +
                          (f" Strongest links: {'; '.join(phrase(r) for r in rels[:3])}." if rels else ""),
                  importance=min(10, len(order) + len(rels) // 2), findings=findings)


def summarize_all() -> dict:
    res = read_json("g2_resolution.json")
    comms = read_json("g3_communities.json")["communities"]
    chunk_access = {c["chunk_id"]: c["access"] for c in read_json("2_chunks.json")}
    ents = {e["id"]: e for e in res["entities"]}

    llm_entity_calls = 0
    entity_summaries = {}
    for e in res["entities"]:
        text, used_llm = _entity_summary(e)
        llm_entity_calls += used_llm
        entity_summaries[e["id"]] = {"summary": text, "from_chunks": e["chunk_ids"], "by": "llm" if used_llm else "copied"}

    reports = []
    for c in sorted(comms, key=lambda c: -c["level"]):          # deepest first
        if c["size"] < COMMUNITY_MIN_SIZE:
            continue
        members = set(c["entity_ids"])
        rels = [r for r in res["relations"] if r["source"] in members and r["target"] in members]
        rels.sort(key=lambda r: -(r["weight"] * r["strength"]))
        rels = rels[:MAX_REL_IN_REPORT]
        ent_lines = "\n".join(f"- {ents[i]['name']} ({ents[i]['type']}): {entity_summaries[i]['summary']}"
                              for i in c["entity_ids"])
        rel_lines = "\n".join(f"- {ents[r['source']]['name']} -[{r['type']}]-> {ents[r['target']]['name']}: "
                              f"{r['descriptions'][0]['text']}" for r in rels)
        if llm.available():
            rep, by = llm.structured(_REPORT_SYS, f"Entities:\n{ent_lines}\n\nRelationships:\n{rel_lines or '(none)'}", Report), "llm"
        else:
            rep, by = template_report(c, ents, rels, entity_summaries), "template"
        from_chunks = sorted({ch for i in c["entity_ids"] for ch in ents[i]["chunk_ids"]})
        reports.append({"community": c["id"], "level": c["level"], "size": c["size"], **rep.model_dump(), "by": by,
                        "from_chunks": from_chunks,
                        "access_groups": sorted({g for ch in from_chunks for g in chunk_access.get(ch, [])})})

    stats = {"entities": len(entity_summaries), "entity_summaries_by_llm": llm_entity_calls,
             "entity_summaries_copied": len(entity_summaries) - llm_entity_calls, "community_reports": len(reports),
             "reports_by_llm": sum(r["by"] == "llm" for r in reports), "llm_available": llm.available()}
    write_json("g4_summaries.json", {"stats": stats, "entities": entity_summaries, "reports": reports})
    return stats
