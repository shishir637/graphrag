"""LLM-based GraphRAG on top of the existing RAG pipeline.

  g1 extract   -> entities + relationships per chunk (LLM, schema-guided, cached)
  g2 resolve   -> merge duplicates (code first, LLM only for borderline pairs)
  g3 community -> Leiden communities, hierarchical (NetworkX, BSD)
  g4 summarize -> entity descriptions + community reports (LLM)
  g5 store     -> Apache AGE (Postgres) as storage only; chunk ids match Qdrant
  g6 search    -> local / global / hybrid (graph chunks fused with the existing hybrid search)
  g7 eval      -> rag vs graph vs hybrid on the same questions, per question type
"""
