"""Step 2 - Chunking: Docling HybridChunker for documents, RecursiveCharacterTextSplitter for plain text.

Output: outputs/2_chunks.json
"""
import hashlib

from .config import CHUNK_MAX_TOKENS, EMBED_MODEL
from .utils import read_json, write_json


def _chunk_id(source: str, headings: list[str], text: str, seen: dict) -> str:
    """Content-based id: the same passage keeps its id when other parts of the document change (so its vectors,
    graph facts and citations stay valid). Identical passages in one document get #2, #3..."""
    key = f"{source}::{' / '.join(headings or [])}::{hashlib.md5(text.encode('utf-8')).hexdigest()}"
    seen[key] = seen.get(key, 0) + 1
    if seen[key] > 1:
        key += f"#{seen[key]}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


def chunk_all() -> list[dict]:
    from docling.chunking import HybridChunker
    from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
    from docling_core.types.doc import DoclingDocument
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    from transformers import AutoTokenizer

    hf_tok = AutoTokenizer.from_pretrained(EMBED_MODEL)
    chunker = HybridChunker(
        tokenizer=HuggingFaceTokenizer(tokenizer=hf_tok, max_tokens=CHUNK_MAX_TOKENS),
        merge_peers=True,
    )
    splitter = RecursiveCharacterTextSplitter.from_huggingface_tokenizer(
        hf_tok, chunk_size=CHUNK_MAX_TOKENS, chunk_overlap=64
    )

    chunks, seen = [], {}
    for doc in read_json("1_parsed.json"):
        base = {"source": doc["source"], "access": doc["access"]}
        if doc["parser"] == "docling":
            dl_doc = DoclingDocument.model_validate(doc["docling_document"])
            for i, ch in enumerate(chunker.chunk(dl_doc)):
                pages = sorted({prov.page_no for item in ch.meta.doc_items for prov in item.prov})
                chunks.append(base | {
                    "chunk_id": _chunk_id(doc["source"], ch.meta.headings or [], ch.text, seen),
                    "headings": ch.meta.headings or [],
                    "pages": pages,
                    "text": ch.text,
                    # contextualize() prefixes the heading path - better for embedding
                    "embed_text": chunker.contextualize(ch),
                })
        else:
            for i, text in enumerate(splitter.split_text(doc["markdown"])):
                chunks.append(base | {
                    "chunk_id": _chunk_id(doc["source"], [], text, seen),
                    "headings": [], "pages": [], "text": text, "embed_text": text,
                })

    for c in chunks:
        c["n_tokens"] = len(hf_tok.encode(c["embed_text"], add_special_tokens=False))
    print(f"  {len(chunks)} chunks, max {max(c['n_tokens'] for c in chunks)} tokens")
    write_json("2_chunks.json", chunks)
    return chunks
