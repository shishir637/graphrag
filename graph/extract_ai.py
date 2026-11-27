"""Optional AI extraction of entities and typed links (graph mode "ai").

Off by default: Context Layer runs no AI model unless an owner turns this on in Settings. Used only while
building the knowledge graph, never for answering. Three kinds of provider:

  anthropic   Claude Messages API (the customer's own key)
  openai      any OpenAI-compatible chat endpoint (OpenAI, Azure-style gateways, vLLM, LM Studio...)
  local       an OpenAI-compatible model on the customer's own machine, e.g. Ollama: no key, data stays inside

Plain HTTPS calls with the standard library: no AI SDK in the product's requirements.
One call per passage, in parallel, cached per passage text + provider + model + prompt version, so a rebuild
only pays for new or changed passages. Output records have the same format as graph/nlp_extract.py, so the
graph builder, merging, communities and search are unchanged. A passage that fails after retries falls back
to local spaCy extraction, so one bad call never stops a build.
"""
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "outputs" / "_graph_cache" / "ai"
PROMPT_VERSION = "ai-v1"
KEY_ENV = "CL_AI_API_KEY"

PROVIDERS = {
    "anthropic": {"label": "Claude (Anthropic API)", "base_url": "https://api.anthropic.com", "model": "claude-haiku-5-5",
                  "needs_key": True, "workers": 8},
    "openai": {"label": "OpenAI-compatible API", "base_url": "https://api.openai.com/v1", "model": "",
               "needs_key": True, "workers": 8},
    "local": {"label": "Local model (Ollama or similar)", "base_url": "http://host.docker.internal:11434/v1", "model": "",
              "needs_key": False, "workers": 2},
}
TIMEOUT = 120
RETRIES = 3


# ---------------------------------------------------------------- configuration
def config() -> dict:
    """Saved settings (app database) with defaults; the key comes from the environment / secrets file."""
    try:
        from app import core
        saved = core.get_setting("ai_extract", {}) or {}
    except Exception:                                   # noqa: BLE001 - used outside the app (scripts)
        saved = {}
    provider = os.environ.get("CL_AI_PROVIDER") or saved.get("provider") or "anthropic"
    d = PROVIDERS.get(provider, PROVIDERS["anthropic"])
    return {
        "provider": provider,
        "model": os.environ.get("CL_AI_MODEL") or saved.get("model") or d["model"],
        "base_url": (os.environ.get("CL_AI_BASE_URL") or saved.get("base_url") or d["base_url"]).rstrip("/"),
        "workers": max(1, min(32, int(os.environ.get("CL_AI_WORKERS") or saved.get("workers") or d["workers"]))),
        "api_key": os.environ.get(KEY_ENV) or "",
    }


def problems(cfg: dict | None = None) -> list[str]:
    cfg = cfg or config()
    out = []
    if cfg["provider"] not in PROVIDERS:
        out.append(f"Unknown provider '{cfg['provider']}'.")
        return out
    if not cfg["model"]:
        out.append("Choose a model name in Settings → AI extraction.")
    if PROVIDERS[cfg["provider"]]["needs_key"] and not cfg["api_key"]:
        out.append("Add an API key in Settings → AI extraction.")
    if not re.match(r"^https?://", cfg["base_url"]):
        out.append("The address must start with http:// or https://.")
    return out


def destination(cfg: dict | None = None) -> str:
    """Where passages are sent, for the build log and Settings."""
    cfg = cfg or config()
    return urlparse(cfg["base_url"]).netloc or cfg["base_url"]


def is_local(cfg: dict | None = None) -> bool:
    host = (urlparse((cfg or config())["base_url"]).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1", "host.docker.internal", "ollama") or host.endswith(".local") \
        or re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", host) is not None


# ---------------------------------------------------------------- prompt
def instructions(schema: dict) -> str:
    ents = "\n".join(f"- {k}: {v.get('description', k)}" for k, v in schema["entity_types"].items())
    rels = "\n".join(f"- {k}: {v.get('description', k)}" for k, v in (schema.get("relation_types") or {}).items())
    if "RELATED_TO" not in (schema.get("relation_types") or {}):
        rels += "\n- RELATED_TO: clearly linked, but no other type fits"
    return (
        "You extract a knowledge graph from one passage of a company's documents.\n\n"
        f"Entity types:\n{ents}\n\nRelation types:\n{rels}\n\n"
        "Rules:\n"
        "- Only named things (people, organisations, places, products, systems, projects, policies...). "
        "No dates, amounts, pronouns or generic nouns ('customers', 'the team').\n"
        "- Use the heading path to resolve references such as 'the company' or 'the department'; add the thing the "
        "section is about as an entity when the passage is about it.\n"
        "- One entity per real thing, with the fullest name used in the passage.\n"
        "- Relations only between your entities, only when the passage states or clearly implies them. Prefer the "
        "most specific type; RELATED_TO only when none fits. Direction: source does the action to target.\n"
        "- Every description is one short sentence taken from the passage, no outside knowledge.\n"
        "- strength: 1-10, how explicit and important the relation is.\n\n"
        'Reply with JSON only: {"entities": [{"name": "", "type": "", "description": ""}], '
        '"relations": [{"source": "", "type": "", "target": "", "description": "", "strength": 5}]}')


def _user(chunk: dict) -> str:
    return (f"<headings>{' > '.join(chunk.get('headings') or [])}</headings>\n"
            f"<passage>\n{chunk['text']}\n</passage>")


# ---------------------------------------------------------------- HTTP
class AIError(RuntimeError):
    pass


def _post(url: str, body: dict, headers: dict, timeout: int | None = None) -> dict:
    data = json.dumps(body).encode("utf-8")
    last = None
    for attempt in range(RETRIES):
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"content-type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            last = AIError(f"{e.code} from {urlparse(url).netloc}: {detail}")
            if e.code in (400, 401, 403, 404):           # will not get better by retrying
                raise last from None
            wait = float(e.headers.get("retry-after") or 0) or 2 ** attempt * 2
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = AIError(f"cannot reach {urlparse(url).netloc}: {getattr(e, 'reason', e)}")
            wait = 2 ** attempt * 2
        time.sleep(min(wait, 30))
    raise last


def _call(cfg: dict, system: str, user: str, timeout: int | None = None) -> tuple[str, dict]:
    """Returns (reply text, usage). timeout: seconds per attempt (a local model on a laptop CPU is slow)."""
    if cfg["provider"] == "anthropic":
        r = _post(cfg["base_url"] + "/v1/messages",
                  {"model": cfg["model"], "max_tokens": 4000, "temperature": 0, "system": system,
                   "messages": [{"role": "user", "content": user}]},
                  {"x-api-key": cfg["api_key"], "anthropic-version": "2023-06-01"}, timeout)
        text = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")
        u = r.get("usage") or {}
        return text, {"input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0)}
    headers = {"authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}
    body = {"model": cfg["model"], "temperature": 0, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if cfg["provider"] == "local":
        body["reasoning_effort"] = "none"     # thinking models (Qwen3.5) answer directly: minutes faster on a CPU
    r = _post(cfg["base_url"] + "/chat/completions", body, headers, timeout)
    text = ((r.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    u = r.get("usage") or {}
    return text, {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)}


def parse_reply(text: str) -> dict:
    """The JSON object in a reply (tolerates code fences or text around it)."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            return json.loads(t[a:b + 1])
        raise AIError("the model did not reply with JSON")


# ---------------------------------------------------------------- extraction
class AIExtractor:
    def __init__(self, cfg: dict | None = None):
        from graph.nlp_extract import clean, head_word_rules, refine, schema
        self.cfg = cfg or config()
        bad = problems(self.cfg)
        if bad:
            raise AIError(" ".join(bad))
        self.schema = schema()
        self.system = instructions(self.schema)
        self.types = {k.lower(): k for k in self.schema["entity_types"]}
        rel = set(self.schema.get("relation_types") or {}) | {"RELATED_TO"}
        self.rel_types = {k.lower(): k for k in rel}
        self._clean, self._refine, self._rules = clean, refine, head_word_rules()
        self.usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0, "cached": 0, "fallbacks": 0}
        self.errors: list[str] = []
        self._lock = threading.Lock()
        self._fallback = None
        self.method = f"ai-{self.cfg['provider']}:{self.cfg['model']}"

    def _key(self, chunk: dict) -> str:
        s = f"{PROMPT_VERSION}|{self.cfg['provider']}|{self.cfg['model']}|{self.system}|{chunk['text']}|{chunk.get('headings')}"
        return hashlib.sha1(s.encode("utf-8")).hexdigest()[:20]

    def normalise(self, raw: dict) -> tuple[list[dict], list[dict]]:
        ents: dict[str, dict] = {}
        for e in raw.get("entities") or []:
            if not isinstance(e, dict):
                continue
            name = self._clean(str(e.get("name") or ""))
            typ = self.types.get(str(e.get("type") or "").strip().lower())
            if not name or len(name) < 2 or not typ:
                continue
            ents.setdefault(name.lower(), {"name": name, "type": self._refine(name, typ, self._rules),
                                           "description": str(e.get("description") or "")[:300]})
        rels = []
        for r in raw.get("relations") or []:
            if not isinstance(r, dict):
                continue
            s = ents.get(self._clean(str(r.get("source") or "")).lower())
            t = ents.get(self._clean(str(r.get("target") or "")).lower())
            if not s or not t or s is t:
                continue
            typ = self.rel_types.get(str(r.get("type") or "").strip().lower(), "RELATED_TO")
            try:
                strength = max(1, min(10, int(r.get("strength") or 5)))
            except (TypeError, ValueError):
                strength = 5
            rels.append({"source": s["name"], "target": t["name"], "type": typ, "strength": strength,
                         "description": str(r.get("description") or f"{s['name']} {typ.lower()} {t['name']}")[:300]})
        return list(ents.values()), rels

    def run(self, chunk: dict) -> dict:
        t0 = time.perf_counter()
        CACHE.mkdir(parents=True, exist_ok=True)
        f = CACHE / f"{self._key(chunk)}.json"
        if f.exists():
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
                with self._lock:
                    self.usage["cached"] += 1
                return rec | {"chunk_id": chunk["chunk_id"], "seconds": 0.0}
            except (ValueError, OSError):                            # damaged cache file: ask again
                pass
        try:
            text, usage = _call(self.cfg, self.system, _user(chunk))
            ents, rels = self.normalise(parse_reply(text))
        except Exception as e:                                       # noqa: BLE001 - fall back, never stop the build
            with self._lock:
                self.usage["fallbacks"] += 1
                if len(self.errors) < 20:
                    self.errors.append(f"{chunk['chunk_id']}: {e}")
            return self._fallback_run(chunk) | {"seconds": round(time.perf_counter() - t0, 3)}
        rec = {"chunk_id": chunk["chunk_id"], "method": self.method, "entities": ents, "relations": rels,
               "usage": usage, "seconds": round(time.perf_counter() - t0, 3)}
        tmp = f.with_name(f"{f.stem}.{threading.get_ident()}.tmp")    # atomic: parallel calls may share a key
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        tmp.replace(f)
        with self._lock:
            self.usage["calls"] += 1
            self.usage["input_tokens"] += usage["input_tokens"]
            self.usage["output_tokens"] += usage["output_tokens"]
        return rec

    def _fallback_run(self, chunk: dict) -> dict:
        with self._lock:
            if self._fallback is None:
                from graph.nlp_extract import Extractor
                self._fallback = Extractor("fast", "cooc")
            ex = self._fallback
        rec = ex.run(chunk)
        rec["method"] = "nlp-fast-cooc (AI call failed)"
        return rec


def test_connection(cfg: dict | None = None) -> dict:
    """One small extraction to check the provider, model and key. Nothing is cached."""
    cfg = cfg or config()
    bad = problems(cfg)
    if bad:
        return {"ok": False, "message": " ".join(bad)}
    from graph.nlp_extract import schema
    sample = {"chunk_id": "test", "headings": ["Company overview"],
              "text": "Maya Rao heads the Grid Analytics department in Pune. Grid Analytics owns the GridSense platform."}
    t0 = time.perf_counter()
    try:
        text, usage = _call(cfg, instructions(schema()), _user(sample))
        raw = parse_reply(text)
    except Exception as e:                                           # noqa: BLE001
        return {"ok": False, "message": str(e)[:400]}
    return {"ok": True, "seconds": round(time.perf_counter() - t0, 2),
            "entities": len(raw.get("entities") or []), "relations": len(raw.get("relations") or []),
            "usage": usage, "message": "Connected."}
