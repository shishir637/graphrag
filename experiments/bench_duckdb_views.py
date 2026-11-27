"""Benchmark for 0.7 Phase 2: the workspace DuckDB as in-memory copies (0.6) vs views over Parquet (0.7).

Writes a synthetic orders table (default 3,000,000 rows) as Parquet (zstd) and as CSV in a temp folder, then for
each way of building the catalog measures: build time, DuckDB memory after the build, and the time of a metric-like
query (sum by city and month). Usage: python experiments/bench_duckdb_views.py [rows]
"""
import sys
import tempfile
import time
from pathlib import Path

import duckdb


def mem(con) -> int:
    return con.execute("SELECT coalesce(sum(memory_usage_bytes), 0) FROM duckdb_memory()").fetchone()[0]


def main(rows: int = 3_000_000):
    d = Path(tempfile.mkdtemp(prefix="cl-bench-"))
    pq = str(d / "orders.parquet").replace("\\", "/")
    gen = duckdb.connect()
    gen.execute(f"""COPY (SELECT i AS order_id, ['Pune','Goa','Delhi','Kochi','Mumbai'][1 + i % 5] AS city,
                  (i % 997) * 1.25 AS amount, DATE '2025-01-01' + CAST(i % 600 AS INTEGER) AS ordered_on
                  FROM range({rows}) t(i)) TO '{pq}' (FORMAT parquet, COMPRESSION zstd)""")
    q = ('SELECT city, date_trunc(\'month\', ordered_on) m, sum(amount) FROM "orders" GROUP BY 1, 2 ORDER BY 1, 2')
    results = {}
    for how in ("copies (0.6)", "views (0.7)"):
        con = duckdb.connect(config={"memory_limit": "1GB"})
        t0 = time.perf_counter()
        kw = "TABLE" if how.startswith("copies") else "VIEW"
        con.execute(f'CREATE {kw} "orders" AS SELECT * FROM read_parquet(\'{pq}\')')
        build = time.perf_counter() - t0
        m = mem(con)
        times = []
        for _ in range(3):
            t1 = time.perf_counter()
            out = con.execute(q).fetchall()
            times.append(time.perf_counter() - t1)
        results[how] = out
        print(f"{how:14s} build {build * 1000:8.1f} ms   memory after build {m / 2**20:8.1f} MiB   "
              f"query median {sorted(times)[1] * 1000:7.1f} ms")
        con.close()
    a, b = results.values()
    print("same results:", a == b, f"({rows:,} rows, Parquet {Path(pq).stat().st_size / 2**20:.1f} MiB)")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 3_000_000)
