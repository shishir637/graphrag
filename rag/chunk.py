"""Step 2 - Chunking: Docling HybridChunker for documents, RecursiveCharacterTextSplitter for plain text.

Output: outputs/2_chunks.json
"""
import hashlib

from .config import CHUNK_MAX_TOKENS, EMBED_MODEL
from .utils import read_json, write_json


def _chunk_id(source: str, i: int) -> str:
    return hashlib.md5(f"{source}::{i}".encode()).hexdigest()[:12]


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

    chunks = []
    for doc in read_json("1_parsed.json"):
        base = {"source": doc["source"], "access": doc["access"]}
        if doc["parser"] == "docling":
            dl_doc = DoclingDocument.model_validate(doc["docling_document"])
            for i, ch in enumerate(chunker.chunk(dl_doc)):
                pages = sorted({prov.page_no for item in ch.meta.doc_items for prov in item.prov})
                chunks.append(base | {
                    "chunk_id": _chunk_id(doc["source"], i),
                    "headings": ch.meta.headings or [],
                    "pages": pages,
                    "text": ch.text,
                    # contextualize() prefixes the heading path - better for embedding
                    "embed_text": chunker.contextualize(ch),
                })
        else:
            for i, text in enumerate(splitter.split_text(doc["markdown"])):
                chunks.append(base | {
                    "chunk_id": _chunk_id(doc["source"], i),
                    "headings": [], "pages": [], "text": text, "embed_text": text,
                })

    for c in chunks:
        c["n_tokens"] = len(hf_tok.encode(c["embed_text"], add_special_tokens=False))
    print(f"  {len(chunks)} chunks, max {max(c['n_tokens'] for c in chunks)} tokens")
    write_json("2_chunks.json", chunks)
    return chunks
