"""One structured-output call used by every graph step, so the provider can be swapped:
Claude (default) or any OpenAI-compatible server such as Ollama running an open model.
Tests replace the backend with set_backend()."""
import json
import os
from typing import Callable, TypeVar

from pydantic import BaseModel

from ..config import GRAPH_LLM, GRAPH_LLM_BASE_URL, GRAPH_LLM_MODEL

T = TypeVar("T", bound=BaseModel)
_backend: Callable | None = None
usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}


def set_backend(fn: Callable | None):
    """fn(system, user, schema) -> schema instance. None restores the configured provider."""
    global _backend
    _backend = fn


def _anthropic(system: str, user: str, schema: type[T]) -> T:
    from ..generate import claude
    resp = claude().messages.parse(model=GRAPH_LLM_MODEL, max_tokens=16000, system=system,
                                   messages=[{"role": "user", "content": user}], output_format=schema)
    usage["input_tokens"] += resp.usage.input_tokens
    usage["output_tokens"] += resp.usage.output_tokens
    if resp.parsed_output is None:
        raise RuntimeError(f"no structured output (stop_reason={resp.stop_reason})")
    return resp.parsed_output


def _openai_compatible(system: str, user: str, schema: type[T]) -> T:
    import urllib.request
    body = {"model": GRAPH_LLM_MODEL, "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()}}}
    req = urllib.request.Request(GRAPH_LLM_BASE_URL.rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        data = json.loads(r.read())
    u = data.get("usage") or {}
    usage["input_tokens"] += u.get("prompt_tokens", 0)
    usage["output_tokens"] += u.get("completion_tokens", 0)
    return schema.model_validate_json(data["choices"][0]["message"]["content"])


def available() -> bool:
    """Is there an LLM to call? Claude needs ANTHROPIC_API_KEY; GRAPH_LLM=openai points at a local
    server (Ollama, vLLM) and needs no key; GRAPH_LLM=none switches the LLM off on purpose.
    Without one, every graph step falls back to a rule-based version instead of failing."""
    if _backend is not None:
        return True
    if GRAPH_LLM == "none":
        return False
    if GRAPH_LLM == "openai":
        return True
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def describe() -> str:
    if not available():
        return "no LLM (rule-based fallbacks)"
    return f"{GRAPH_LLM}: {GRAPH_LLM_MODEL}" if _backend is None else "test backend"


def structured(system: str, user: str, schema: type[T]) -> T:
    usage["calls"] += 1
    if _backend is not None:
        return _backend(system, user, schema)
    return (_openai_compatible if GRAPH_LLM == "openai" else _anthropic)(system, user, schema)
