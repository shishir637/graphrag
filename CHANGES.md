# Changes

## 0.2.0 (25 Nov 2026): no AI model inside

Context Layer no longer calls Claude (or any language model). It is the context layer only: it finds the
evidence and serves it over MCP; the agent that connects (Claude, ChatGPT, Cursor, Copilot, a company bot)
writes the answer. There is no API key to set.

**Answering**
- `ask` returns evidence: passages with highlighted sentences, graph links with the sentence behind each,
  or metric rows with the SQL. A rule-based router (`app/router.py`) picks documents, metrics, connections
  or overview; callers can force a mode.
- Retrieval adds knowledge-graph candidates before re-ranking and keeps one passage per entity the
  question names (comparisons, multi-part questions).
- New MCP tools: `describe_data` (metrics, dimensions with values, synonyms, business rules, examples),
  `find_connections`, `explore_entity`. `query_metrics` now takes metric and dimension names filled in by
  the agent (or a plain question).

**Metrics**
- A rule-based planner (`semantic/rule_planner.py`) turns plain questions into metric requests: typo- and
  plural-tolerant matching, join-aware column choice, fan-out swaps, ranking, dates, refusals.
- Tables can be CSV, Excel (every sheet), JSON (nested fields flattened), JSON Lines or Parquet
  (`semantic/ingest.py`).
- New tables get draft definitions from the data profile (`semantic/auto_model.py`): entities, joins,
  count/total/average metrics and synonyms. Agents see only approved metrics; admins approve on the Metrics page.

**Knowledge graph**
- Built without an LLM (`graph/nlp_extract.py`): GLiNER + spaCy entities (accurate) or spaCy only (fast);
  links from co-occurrence in a sentence and from section topics (the approach of Microsoft's LazyGraphRAG);
  optional GLiNER2 typed relations. Entity merging is rule-based.
- Answering reads the graph build file (`graph/search.py`), so it works when the graph database is down;
  Apache AGE keeps a copy for Cypher.

**Documents**
- JSON / JSON Lines documents are searchable (one block per record); CSV documents go through Docling.
- Embedding is incremental: unchanged passages keep their vectors.

**Quality page**: free, deterministic benchmarks (no judge model): retrieval recall/MRR/nDCG on gold
passages, metric execution accuracy on gold requests, entity and link extraction on hand-labelled passages.

**Removed from the product path**: `anthropic`, RAGAS and the LangChain LLM wrappers (now in
`requirements-research.txt` for comparisons only). Settings shows the MCP link instead of an API key.
