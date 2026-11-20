"""Shared Claude client. Kept separate from generate.py so code that only needs Claude
(semantic layer, evaluation) does not import Qdrant and the retrieval models."""
import os

import anthropic

_client = None


def workspace_headers() -> dict:
    """User-level keys (sk-ant-usr-...) must name a workspace; set ANTHROPIC_WORKSPACE_ID in .env."""
    ws = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    return {"anthropic-workspace-id": ws} if ws else {}


def claude() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(default_headers=workspace_headers())
    return _client
