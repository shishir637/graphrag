"""Data-quality checks run before numbers are served (DuckDB only - free, milliseconds).

  freshness     latest order date vs. today: stale data is reported with every answer
  keys          primary keys are unique and not null
  orphans       facts pointing at missing dimension rows (orders without a customer, items without a product)
  nulls         share of empty values in columns metrics group by

Results are cached per process; `rag.py sl-quality` prints them.
"""
from datetime import date
from functools import lru_cache

from .db import connect

FRESHNESS_DAYS = 7


@lru_cache
def checks(today: date) -> dict:
    con = connect()
    q = lambda sql: con.execute(sql).fetchone()                                   # noqa: E731
    latest = q("SELECT max(order_date) FROM orders")[0]
    stale_days = (today - latest).days if latest else None
    out = {
        "latest_order_date": str(latest), "stale_days": stale_days,
        "duplicate_keys": {t: q(f"SELECT count(*) - count(DISTINCT {k}) FROM {t}")[0]
                           for t, k in (("orders", "order_id"), ("customers", "customer_id"),
                                        ("products", "product_id"), ("order_items", "order_item_id"))},
        "orphans": {
            "orders_without_customer": q("SELECT count(*) FROM orders o LEFT JOIN customers c USING (customer_id) "
                                         "WHERE c.customer_id IS NULL")[0],
            "items_without_product": q("SELECT count(*) FROM order_items i LEFT JOIN products p USING (product_id) "
                                       "WHERE p.product_id IS NULL")[0],
            "items_without_order": q("SELECT count(*) FROM order_items i LEFT JOIN orders o USING (order_id) "
                                     "WHERE o.order_id IS NULL")[0],
        },
        "null_share": {c: round(q(f"SELECT avg(CASE WHEN {c} IS NULL THEN 1 ELSE 0 END) FROM {t}")[0], 4)
                       for t, c in (("orders", "channel"), ("orders", "status"), ("customers", "city"),
                                    ("customers", "segment"))},
    }
    warnings = []
    if stale_days is not None and stale_days > FRESHNESS_DAYS:
        warnings.append(f"data is {stale_days} days old (latest order {latest})")
    for t, n in out["duplicate_keys"].items():
        if n:
            warnings.append(f"{n} duplicate keys in {t}")
    for k, n in out["orphans"].items():
        if n:
            warnings.append(f"{n} {k.replace('_', ' ')} (shown as an empty region/city/product)")
    out["warnings"] = warnings
    return out
