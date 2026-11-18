"""Step 1 - Parsing: raw files -> clean structured text (Docling).

Output: outputs/1_parsed.json
"""
import json

from .config import DOCS_DIR, DOCLING_EXTS, PLAINTEXT_EXTS
from .utils import timer, write_json


def load_access_map() -> dict:
    """Optional documents/_access.json: {"file.pdf": ["hr", "admin"]}. Default group is "public"."""
    path = DOCS_DIR / "_access.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def parse_all() -> list[dict]:
    from docling.document_converter import DocumentConverter

    converter = DocumentConverter()
    access = load_access_map()
    results, timings = [], {}

    files = sorted(p for p in DOCS_DIR.iterdir() if p.is_file() and not p.name.startswith("_"))
    if not files:
        raise SystemExit(f"No documents found in {DOCS_DIR}")

    for path in files:
        ext = path.suffix.lower()
        entry = {"source": path.name, "access": access.get(path.name, ["public"])}
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
            elif ext in PLAINTEXT_EXTS:
                entry |= {"parser": "plaintext", "markdown": path.read_text(encoding="utf-8", errors="replace")}
            else:
                print(f"  skipping unsupported file: {path.name}")
                continue
        entry["seconds"] = timings[path.name]
        print(f"  parsed {path.name} ({entry['parser']}, {entry['seconds']}s)")
        results.append(entry)

    write_json("1_parsed.json", results)
    return results
