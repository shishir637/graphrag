"""Apache AGE (openCypher) research tools, moved out of the product in 0.7: the product keeps its knowledge graph in
plain Postgres tables (graph/kg.py) and never imports these. store.py writes a graph copy into AGE, query.py and
evaluate.py are the LLM GraphRAG research CLI (rag.py gr-ask / gr-eval). Needs the AGE database
(experiments/docker-compose.graph.yml) and, for query/evaluate, a Claude key."""
