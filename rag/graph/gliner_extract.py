"""Local entity and relationship extraction with GLiNER2 (Apache 2.0 code and weights). No API calls.

GLiNER2 is a small encoder model that finds spans of any entity type you name, and pairs of
spans for any relation you name, each with a confidence score. Our entity and relation types and
their descriptions (config.py) are its input, so it returns only types the schema declares.

GLiNER2 gives no description of an entity. We use the sentence the entity appears in, which
is what the LLM would have paraphrased anyway, so later steps (summaries, search) still have text.

Raw output of model.extract(text, schema, include_confidence=True, include_spans=True):
  {"entities": {label: [{"text", "start", "end", "confidence"}]},
   "relation_extraction": {label: [{"head": {...span...}, "tail": {...span...}}]}}
"""
import re

from ..config import (ENTITY_DESCRIPTIONS, ENTITY_TYPES, GLINER_ENTITY_THRESHOLD, GLINER_MODEL,
                      GLINER_RELATION_THRESHOLD, RELATION_DESCRIPTIONS, RELATION_TYPES)

_model = None
_SENT = re.compile(r"[^.!?\n]+[.!?]?")


class GlinerMissing(SystemExit):
    pass


def set_model(model):
    """Tests pass a stand-in with the same create_schema()/extract() interface."""
    global _model, _schema
    _model, _schema = model, None


def model():
    global _model
    if _model is None:
        try:
            from gliner2 import AutoExtractor
        except ImportError:
            raise GlinerMissing('GLiNER2 is not installed. Run: pip install "gliner2[local]" '
                                "(or set EXTRACT_MODE=llm to use the LLM for every chunk).") from None
        print(f"   loading {GLINER_MODEL} (first run downloads it)")
        _model = AutoExtractor.from_pretrained(GLINER_MODEL)
    return _model


_schema = None


def schema():
    global _schema
    if _schema is None:
        m = model()
        rel_floor = min(GLINER_RELATION_THRESHOLD, GLINER_ENTITY_THRESHOLD)
        _schema = (m.create_schema()
                   .entities({t: ENTITY_DESCRIPTIONS.get(t, t) for t in ENTITY_TYPES})
                   .relations({r: {"description": RELATION_DESCRIPTIONS.get(r, r), "threshold": rel_floor}
                               for r in RELATION_TYPES}))
    return _schema


def _sentence(text: str, start: int, end: int) -> str:
    for m in _SENT.finditer(text):
        if m.start() <= start < m.end() or m.start() < end <= m.end():
            return " ".join(m.group().split())
    return " ".join(text[max(0, start - 80):end + 80].split())


def _trim(text: str, start: int, end: int):
    if not 0 <= start < end <= len(text):
        return None
    w = text[start:end]
    lead = len(w) - len(w.lstrip())
    s, e = start + lead, start + len(w.rstrip())
    return (s, e) if s < e else None


def decode(text: str, raw: dict) -> dict:
    """Raw GLiNER2 output -> our entities and relations, with what was dropped and why."""
    best = {}                                  # span -> (confidence, label); one label per span, highest wins
    for label, items in (raw.get("entities") or {}).items():
        if label not in ENTITY_TYPES:
            continue
        for it in items:
            span = _trim(text, it["start"], it["end"])
            if span is None:
                continue
            conf = float(it.get("confidence", 1.0))
            if span not in best or (conf, label) > best[span]:
                best[span] = (conf, label)
    ents, dropped = [], []
    for (s, e), (conf, label) in sorted(best.items()):
        name = " ".join(text[s:e].split())
        if conf < GLINER_ENTITY_THRESHOLD:
            dropped.append(f"entity '{name}' ({label}) {conf:.2f} below {GLINER_ENTITY_THRESHOLD}")
            continue
        ents.append({"name": name, "type": label, "description": _sentence(text, s, e),
                     "confidence": round(conf, 3), "start": s, "end": e})

    by_span = {(x["start"], x["end"]): i for i, x in enumerate(ents)}
    by_text = {}
    for i, x in enumerate(ents):
        by_text.setdefault(x["name"].casefold(), []).append(i)

    def endpoint(ep):
        span = _trim(text, ep["start"], ep["end"]) if "start" in ep else None
        if span in by_span:
            return by_span[span]
        hits = by_text.get(" ".join(str(ep.get("text", "")).split()).casefold(), [])
        return hits[0] if len(hits) == 1 else None

    kept = {}
    for label, pairs in (raw.get("relation_extraction") or {}).items():
        if label not in RELATION_TYPES:
            continue
        for p in pairs:
            conf = float(p["head"].get("confidence", 1.0))
            si, ti = endpoint(p["head"]), endpoint(p["tail"])
            desc = f"{p['head'].get('text', '')} -{label}-> {p['tail'].get('text', '')}"
            if si is not None and si == ti:
                continue
            if conf < GLINER_RELATION_THRESHOLD:
                dropped.append(f"relation {desc} {conf:.2f} below {GLINER_RELATION_THRESHOLD}")
            elif si is None or ti is None:
                dropped.append(f"relation {desc}: an endpoint is not a kept entity")
            else:
                kept[(si, ti, label)] = max(kept.get((si, ti, label), 0.0), conf)
    rels = []
    for (si, ti, label), conf in sorted(kept.items()):
        a, b = ents[si], ents[ti]
        rels.append({"source": a["name"], "target": b["name"], "type": label,
                     "description": _sentence(text, min(a["start"], b["start"]), max(a["end"], b["end"])),
                     "strength": max(1, min(10, round(conf * 10))), "confidence": round(conf, 3)})
    return {"entities": ents, "relations": rels, "dropped": dropped}


def extract_local(text: str) -> dict:
    raw = model().extract(text, schema(), threshold=GLINER_ENTITY_THRESHOLD,     # one cut: GLiNER resolves overlaps after thresholding
                          include_confidence=True, include_spans=True)
    return decode(text, raw)
