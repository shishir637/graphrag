"""Step 1 - Parsing: raw files -> clean structured text (Docling).

Output: outputs/1_parsed.json
"""
import json

import hashlib

from .config import DOCS_DIR, DOCLING_EXTS, OUT_DIR, PLAINTEXT_EXTS, RECORD_EXTS

CACHE_DIR = OUT_DIR / "_parse_cache"   # parsed files by content hash: unchanged files are not parsed again
from .utils import timer, write_json


def load_access_map() -> dict:
    """Access groups per file (rag/registry.py: Postgres in the app, documents/_access.json on the command line).
    Default group is "public"."""
    from .registry import access_map
    return access_map()


def _flatten(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _flatten(v, f"{prefix}{k}." if not isinstance(v, (dict, list)) or isinstance(v, dict) else f"{prefix}{k}")
    elif isinstance(obj, list):
        if all(not isinstance(x, (dict, list)) for x in obj):
            yield prefix.rstrip("."), ", ".join(map(str, obj))
        else:
            for i, x in enumerate(obj, 1):
                yield from _flatten(x, f"{prefix}[{i}].")
    else:
        yield prefix.rstrip("."), obj


def records_to_text(path) -> str:
    """JSON / JSON Lines -> readable text: one block per record, one "field: value" line per field, so a
    search for a value finds the record it belongs to."""
    raw = path.read_text(encoding="utf-8-sig")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if isinstance(data, dict):
        lists = {k: v for k, v in data.items() if isinstance(v, list) and v and all(isinstance(x, dict) for x in v)}
        groups = lists or {"": [data]}
    else:
        groups = {"": data}
    blocks = []
    for name, recs in groups.items():
        for i, r in enumerate(recs, 1):
            title = f"{path.stem}{' / ' + name if name else ''} record {i}"
            lines = [f"{k}: {v}" for k, v in _flatten(r) if v not in (None, "")]
            blocks.append(title + "\n" + "\n".join(lines))
    return "\n\n".join(blocks)


def parse_all() -> list[dict]:
    from docling.document_converter import DocumentConverter

    converter = DocumentConverter()
    access = load_access_map()
    results, timings = [], {}

    files = sorted(p for p in DOCS_DIR.iterdir() if p.is_file() and not p.name.startswith("_"))
    if not files:
        raise SystemExit(f"No documents found in {DOCS_DIR}")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    used = set()
    for path in files:
        ext = path.suffix.lower()
        entry = {"source": path.name, "access": access.get(path.name, ["public"])}
        digest = hashlib.sha256(path.read_bytes() + ext.encode()).hexdigest()
        cached = CACHE_DIR / f"{digest}.json"
        used.add(cached.name)
        if cached.exists():
            try:
                entry |= json.loads(cached.read_text(encoding="utf-8")) | {"seconds": 0.0}
                print(f"  unchanged {path.name} (parsed before)")
                results.append(entry)
                continue
            except (OSError, ValueError):
                pass
        with timer(path.name, timings):
            if ext in DOCLING_EXTS:
                doc = converter.convert(path).document
                entry |= {
                    "parser": "docling",
                    "pages": len(doc.pages) if doc.pages else None,
                    "tables": len(doc.tables),
                    "markdown": doc.export_to_markdown(),
                    "docling_document": doc.export_to_dict(),  # kept so HybridChunker can use the structure
                }
            elif ext in RECORD_EXTS:
                entry |= {"parser": "plaintext", "markdown": records_to_text(path)}
            elif ext in PLAINTEXT_EXTS:
                entry |= {"parser": "plaintext", "markdown": path.read_text(encoding="utf-8", errors="replace")}
            else:
                print(f"  skipping unsupported file: {path.name}")
                continue
        entry["seconds"] = timings[path.name]
        print(f"  parsed {path.name} ({entry['parser']}, {entry['seconds']}s)")
        results.append(entry)
        keep = {k: v for k, v in entry.items() if k not in ("source", "access", "seconds")}
        cached.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
    for old in CACHE_DIR.glob("*.json"):                 # forget files that were removed or changed
        if old.name not in used:
            old.unlink(missing_ok=True)

    write_json("1_parsed.json", results)
    return results
