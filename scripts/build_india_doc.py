"""Build a ~1000-page test document about India from Wikipedia (text licensed CC BY-SA 4.0).

For the whole country and for each of the 28 states and 8 union territories it fetches the main
article plus sub-articles (history, geography, economy, culture, ...) as plain text, drops
reference-type sections, and writes one Word file with real headings:

  Heading 1  state / union territory        Heading 2  article        Heading 3+  article sections

Each state gets about the same word budget, so no state dominates the index.

  python scripts/build_india_doc.py [--pages 1000]

Output: documents/India - states and union territories.docx
        outputs/india_doc_manifest.json   (articles used, words per state, revision ids)
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests
from docx import Document
from docx.shared import Pt

ROOT = Path(__file__).resolve().parent.parent
OUT_DOC = ROOT / "documents" / "India - states and union territories.docx"
MANIFEST = ROOT / "outputs" / "india_doc_manifest.json"
CACHE = ROOT / "outputs" / "_wiki_cache"
API = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "rag-eval-test-corpus/0.1 (educational document builder)"}
WORDS_PER_PAGE = 500                    # 11 pt single-spaced Word page

NATIONAL = ["India", "States and union territories of India", "History of India", "Geography of India",
            "Economy of India", "Politics of India", "Culture of India"]
STATES = ["Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat", "Haryana",
          "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur",
          "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab, India", "Rajasthan", "Sikkim", "Tamil Nadu",
          "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal"]
UTS = ["Andaman and Nicobar Islands", "Chandigarh", "Dadra and Nagar Haveli and Daman and Diu", "Delhi",
       "Jammu and Kashmir (union territory)", "Ladakh", "Lakshadweep", "Puducherry"]
SUB = ["History of {}", "Geography of {}", "Economy of {}", "Culture of {}", "Politics of {}",
       "Tourism in {}", "Education in {}", "Demographics of {}", "Transport in {}", "Cuisine of {}"]
EXTRA = {  # regions whose own sub-articles are thin on Wikipedia
    "Dadra and Nagar Haveli and Daman and Diu": ["Dadra and Nagar Haveli", "Daman and Diu", "Daman, India",
                                                 "Silvassa", "Diu, India"],
    "Andaman and Nicobar Islands": ["Andaman Islands", "Nicobar Islands", "Port Blair"],
    "Lakshadweep": ["Kavaratti", "Agatti Island", "Minicoy"],
}
SKIP_SECTIONS = {"see also", "references", "notes", "external links", "further reading", "bibliography",
                 "citations", "sources", "footnotes", "notes and references", "explanatory notes", "gallery"}


def short(name: str) -> str:
    return {"Punjab, India": "Punjab", "Jammu and Kashmir (union territory)": "Jammu and Kashmir"}.get(name, name)


def fetch(title: str) -> dict | None:
    """Plain-text extract of one article (cached on disk). None if the article does not exist."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / (re.sub(r"[^\w-]+", "_", title) + ".json")
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    params = {"action": "query", "format": "json", "formatversion": 2, "prop": "extracts|revisions",
              "rvprop": "ids", "explaintext": 1, "exsectionformat": "wiki", "redirects": 1, "titles": title}
    for attempt in range(8):
        try:
            r = requests.get(API, params=params, headers=HEADERS, timeout=60)
            if r.status_code == 429:                  # rate limited: wait as long as the server asks
                time.sleep(int(r.headers.get("retry-after", 5)) + 2 * attempt)
                continue
            r.raise_for_status()
            break
        except requests.RequestException:
            time.sleep(3 * (attempt + 1))
    else:
        raise RuntimeError(f"could not fetch {title}")
    page = r.json()["query"]["pages"][0]
    data = None if page.get("missing") or not page.get("extract") else {
        "title": page["title"], "revid": page["revisions"][0]["revid"], "text": page["extract"]}
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    time.sleep(1.5)                                   # be polite to the API
    return data


def sections(text: str) -> list[tuple[int, str, list[str]]]:
    """Split '== Heading ==' plain text into (level, heading, paragraphs); drop reference sections."""
    out, level, head, paras, skip_below = [], 1, "", [], None
    for line in text.splitlines():
        m = re.match(r"^(={2,6})\s*(.*?)\s*\1$", line.strip())
        if m:
            if paras and skip_below is None:
                out.append((level, head, paras))
            level, head, paras = len(m.group(1)), m.group(2), []
            if skip_below is not None and level <= skip_below:
                skip_below = None
            if head.lower() in SKIP_SECTIONS:
                skip_below = level
            continue
        line = line.strip()
        if line and skip_below is None:
            paras.append(line)
    if paras and skip_below is None:
        out.append((level, head, paras))
    return out


def words(secs) -> int:
    return sum(len(p.split()) for _, _, ps in secs for p in ps)


def trim(secs, budget: int):
    """Keep whole sections in order until the word budget is used."""
    kept, used = [], 0
    for s in secs:
        n = words([s])
        if used + n > budget and kept:
            break
        kept.append(s)
        used += n
    return kept


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=1000)
    target = ap.parse_args().pages * WORDS_PER_PAGE

    plan = [("India (national overview)", NATIONAL)]
    plan += [(short(s), [s] + EXTRA.get(s, []) + [t.format(short(s)) for t in SUB]) for s in STATES + UTS]

    national_budget = int(target * 0.08)
    per_region = (target - national_budget) // (len(plan) - 1)

    doc = Document()
    doc.styles["Normal"].font.size = Pt(11)
    doc.add_heading("India: States and Union Territories", 0)
    doc.add_paragraph("A reference compilation covering India as a whole and each of its 28 states and 8 union "
                      "territories: history, geography, economy, politics, culture, education, transport and more.")
    doc.add_paragraph("Source: English Wikipedia articles (revision ids in outputs/india_doc_manifest.json), "
                      "licensed CC BY-SA 4.0. Compiled for testing a retrieval system; text lightly reformatted.")

    manifest, total = {"source": "en.wikipedia.org, CC BY-SA 4.0", "regions": {}}, 0
    for region, titles in plan:
        budget = national_budget if region.startswith("India") else per_region
        print(f"  {region}: ", end="", flush=True)
        doc.add_page_break()
        doc.add_heading(region, 1)
        used, arts = 0, []
        for t in titles:
            if used >= budget * 0.97:
                break
            a = fetch(t)
            if a is None or any(x["title"] == a["title"] for x in arts):
                continue
            secs = trim(sections(a["text"]), budget - used)
            n = words(secs)
            if n < 150:
                continue
            doc.add_heading(a["title"], 2)
            for level, head, paras in secs:
                if head:
                    doc.add_heading(head, min(level + 1, 6))       # '==' -> Heading 3, '===' -> Heading 4
                for p in paras:
                    doc.add_paragraph(p)
            used += n
            arts.append({"title": a["title"], "revid": a["revid"], "words": n})
        total += used
        manifest["regions"][region] = {"words": used, "articles": arts}
        print(f"{used:,} words from {len(arts)} articles")

    manifest |= {"total_words": total, "approx_pages": round(total / WORDS_PER_PAGE)}
    OUT_DOC.parent.mkdir(exist_ok=True)
    doc.save(OUT_DOC)
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  {total:,} words (~{total // WORDS_PER_PAGE} pages) -> {OUT_DOC.relative_to(ROOT)}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
