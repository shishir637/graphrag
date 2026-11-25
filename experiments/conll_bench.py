"""CoNLL-2003 English test set (PER, ORG, LOC; MISC left out because neither model's label set matches it):
span-level exact-match micro F1 on the first N sentences. Usage: python conll_bench.py path/eng.testb N"""
import re, sys, time
from collections import Counter
path, N = sys.argv[1], int(sys.argv[2])
sents, cur = [], []
for line in open(path, encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("-DOCSTART-"):
        if cur: sents.append(cur); cur = []
        continue
    w, *_, tag = line.split()
    cur.append((w, tag))
sents = [s for s in sents if len(s) > 3][:N]

def spans(tags):          # IOB1 -> (start, end, type)
    out, start, typ = [], None, None
    for i, t in enumerate(tags + ["O"]):
        p, ty = (t.split("-") + [None])[:2] if t != "O" else ("O", None)
        if start is not None and (t == "O" or p == "B" or ty != typ):
            out.append((start, i, typ)); start = None
        if t != "O" and start is None:
            start, typ = i, ty
    return out

def char_to_tok(words):
    text, offs = "", []
    for w in words:
        if text: text += " "
        offs.append((len(text), len(text) + len(w))); text += w
    return text, offs

def to_tok(offs, s, e):
    toks = [i for i, (a, b) in enumerate(offs) if a < e and b > s]
    return (toks[0], toks[-1] + 1) if toks else None

def score(pred_fn, label):
    tp = fp = fn = 0; t0 = time.perf_counter()
    for s in sents:
        words = [w for w, _ in s]; gold = {g for g in spans([t for _, t in s]) if g[2] in ("PER", "ORG", "LOC")}
        text, offs = char_to_tok(words)
        pred = set()
        for a, b, ty in pred_fn(text):
            t = to_tok(offs, a, b)
            if t and ty: pred.add((t[0], t[1], ty))
        tp += len(gold & pred); fp += len(pred - gold); fn += len(gold - pred)
    p = tp / (tp + fp) if tp + fp else 0; r = tp / (tp + fn) if tp + fn else 0
    print(f"{label:<28} P {p:.3f} R {r:.3f} F1 {2*p*r/(p+r):.3f}  ({(time.perf_counter()-t0)/len(sents)*1000:.0f} ms/sentence, {len(sents)} sentences)")

import spacy
nlp = spacy.load("en_core_web_lg")
SP = {"PERSON": "PER", "ORG": "ORG", "GPE": "LOC", "LOC": "LOC", "FAC": "LOC", "NORP": None}
score(lambda t: [(e.start_char, e.end_char, SP.get(e.label_)) for e in nlp(t).ents], "spaCy en_core_web_lg")
if len(sys.argv) > 3: sys.exit()
from gliner import GLiNER
m = GLiNER.from_pretrained("urchade/gliner_medium-v2.1")
GL = {"person": "PER", "organization": "ORG", "location": "LOC"}
score(lambda t: [(e["start"], e["end"], GL[e["label"]]) for e in m.predict_entities(t, list(GL), threshold=0.5)], "GLiNER medium v2.1 (t=0.5)")
