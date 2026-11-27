"""Benchmark for 0.7 Phase 3: passage texts in the Qdrant payload (0.6) vs fetched from Postgres by id (0.7).

The part of retrieval that changes is "get the candidates' texts": 0.6 returned them with the search results
(with_payload=True, text included); 0.7 returns ids and filter fields from Qdrant and reads the texts in one
batched Postgres query. This measures exactly that on N synthetic passages (~1,200 characters, 1024-d vectors),
top 50 candidates per query, median of 40 queries. Needs a Qdrant server and a Postgres (the test containers).

  python experiments/bench_passage_texts.py [passages]
Env: CL_TEST_QDRANT_URL (http://127.0.0.1:6399), CL_TEST_DATABASE_URL (postgresql://rag:testpw@127.0.0.1:5499/contextlayer)
"""
import os
import random
import statistics
import sys
import time
import uuid

import psycopg
from qdrant_client import QdrantClient, models

Q = os.environ.get("CL_TEST_QDRANT_URL", "http://127.0.0.1:6399")
PG = os.environ.get("CL_TEST_DATABASE_URL", "postgresql://rag:testpw@127.0.0.1:5499/contextlayer")
COLL = "bench_passage_texts"
DIM, TOP = 1024, 50


def main(n: int = 20000):
    rnd = random.Random(7)
    words = "contract notice renewal invoice amount vendor policy leave employee incident payroll service".split()
    texts = {f"c{i}": " ".join(rnd.choice(words) for _ in range(170))[:1200] for i in range(n)}
    qc = QdrantClient(url=Q, timeout=60)
    if qc.collection_exists(COLL):
        qc.delete_collection(COLL)
    qc.create_collection(COLL, vectors_config={"dense": models.VectorParams(size=DIM, distance=models.Distance.COSINE)})
    ids = list(texts)
    for i in range(0, n, 512):
        qc.upsert(COLL, wait=True, points=[models.PointStruct(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, c)), vector={"dense": [rnd.random() for _ in range(DIM)]},
            payload={"chunk_id": c, "source": "doc.md", "access": ["public"], "headings": ["H"], "text": texts[c]})
            for c in ids[i:i + 512]])
    with psycopg.connect(PG, autocommit=True) as pg:
        pg.execute("DROP TABLE IF EXISTS bench_passages; CREATE TABLE bench_passages (tenant_id text, chunk_id text, "
                   "text text, PRIMARY KEY (tenant_id, chunk_id))")
        with pg.cursor().copy("COPY bench_passages FROM STDIN") as cp:
            for c, t in texts.items():
                cp.write_row(("t1", c, t))
        queries = [[rnd.random() for _ in range(DIM)] for _ in range(40)]
        old, new = [], []
        for qv in queries:
            t0 = time.perf_counter()
            pts = qc.query_points(COLL, query=qv, using="dense", limit=TOP, with_payload=True).points
            got = {p.payload["chunk_id"]: p.payload["text"] for p in pts}
            old.append(time.perf_counter() - t0)
            t0 = time.perf_counter()
            pts = qc.query_points(COLL, query=qv, using="dense", limit=TOP,
                                  with_payload=["chunk_id", "source", "access", "headings"]).points
            want = [p.payload["chunk_id"] for p in pts]
            rows = pg.execute("SELECT chunk_id, text FROM bench_passages WHERE tenant_id = %s AND chunk_id = ANY(%s)",
                              ("t1", want)).fetchall()
            got2 = dict(rows)
            new.append(time.perf_counter() - t0)
            assert got2 == got
        pg.execute("DROP TABLE bench_passages")
    qc.delete_collection(COLL)
    ms = lambda xs: statistics.median(xs) * 1000           # noqa: E731
    print(f"{n:,} passages, top {TOP}: text in payload {ms(old):.1f} ms | ids from Qdrant + texts from Postgres "
          f"{ms(new):.1f} ms (median of {len(queries)})")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 20000)
