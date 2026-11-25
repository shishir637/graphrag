# Context Layer database: PostgreSQL 17 with Apache AGE (knowledge-graph copy, openCypher) and pgvector
# (installed for later use; vectors live in Qdrant today).
FROM apache/age:release_PG17_1.7.0
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-17-pgvector \
    && rm -rf /var/lib/apt/lists/*
