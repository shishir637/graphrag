"""Shared Claude client. Kept separate from generate.py so code that only needs Claude
(semantic layer, evaluation) does not import Qdrant and the retrieval models."""
import os
from pathlib import Path

import anthropic

_client = None


def load_dotenv():
    """KEY=value lines from the project's .env, without overriding variables already set in the shell.
    Done here so every entry point (rag.py, python -m graph..., tests) finds ANTHROPIC_API_KEY."""
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()


def workspace_headers() -> dict:
    """User-level keys (sk-ant-usr-...) must name a workspace; set ANTHROPIC_WORKSPACE_ID in .env."""
    ws = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    return {"anthropic-workspace-id": ws} if ws else {}


def claude() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(default_headers=workspace_headers())
    return _client
