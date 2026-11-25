"""Generate a small, realistic sales dataset in data/ plus a 'finance report' in reference/.

Tables: regions, customers, products, orders, order_items (CSV).
Business rules baked in, so metrics need real definitions:
  - orders.status is completed / cancelled / returned; cancelled orders never count
  - returned orders carry a refund_amount (full or partial)
  - orders.amount = sum of its order_items line totals
  - two orders point at a customer that no longer exists (a realistic data quality issue)
The finance report is computed separately with pandas from the raw rows, the way a finance team
would, so the semantic layer's numbers can be checked against it.
"""
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .config import DATA_DIR, REFERENCE_DIR

START, END = date(2024, 4, 1), date(2026, 11, 18)
CITIES = {  # city -> region_id
    "Delhi": "R01", "Chandigarh": "R01", "Jaipur": "R01",
    "Bengaluru": "R02", "Chennai": "R02", "Hyderabad": "R02", "Kochi": "R02",
    "Mumbai": "R03", "Pune": "R03", "Ahmedabad": "R03",
    "Kolkata": "R04", "Bhubaneswar": "R04",
}
REGIONS = [("R01", "North"), ("R02", "South"), ("R03", "West"), ("R04", "East")]
CATEGORIES = {
    "Electronics": (1500, 45000), "Home": (300, 8000), "Office": (100, 6000),
    "Apparel": (400, 5000), "Books": (150, 1200),
}
FIRST = ["Aarav", "Diya", "Ishaan", "Meera", "Rohan", "Ananya", "Kabir", "Saanvi", "Vikram", "Priya",
         "Arjun", "Nisha", "Rahul", "Kavya", "Siddharth", "Lakshmi", "Aditya", "Pooja", "Karthik", "Sneha"]
LAST = ["Sharma", "Iyer", "Reddy", "Patel", "Nair", "Gupta", "Menon", "Singh", "Rao", "Das", "Joshi", "Khan"]


def _days(a: date, b: date) -> int:
    return (b - a).days


def generate(n_customers: int = 600, n_orders: int = 6000, seed: int = 7) -> dict:
    rng = np.random.default_rng(seed)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)

    regions = pd.DataFrame(REGIONS, columns=["region_id", "region_name"]).assign(country="India")

    city_names = list(CITIES)
    cust = []
    for i in range(1, n_customers + 1):
        f, l = rng.choice(FIRST), rng.choice(LAST)
        city = rng.choice(city_names, p=np.array([3, 1, 1, 4, 3, 3, 1, 4, 2, 1, 2, 1]) / 26)
        signup = date(2024, 1, 1) + timedelta(days=int(rng.integers(0, _days(date(2024, 1, 1), END) - 30)))
        cust.append({
            "customer_id": f"C{i:04d}", "customer_name": f"{f} {l}",
            "email": None if rng.random() < 0.02 else f"{f.lower()}.{l.lower()}{i}@example.com",
            "city": city, "region_id": CITIES[city],
            "segment": rng.choice(["Consumer", "Small Business", "Enterprise"], p=[0.7, 0.22, 0.08]),
            "signup_date": signup,
        })
    customers = pd.DataFrame(cust)

    prods = []
    for i in range(1, 41):
        cat = list(CATEGORIES)[(i - 1) % len(CATEGORIES)]
        lo, hi = CATEGORIES[cat]
        prods.append({"product_id": f"P{i:03d}", "product_name": f"{cat} item {i:02d}", "category": cat,
                      "unit_price": round(float(rng.uniform(lo, hi)), -1)})
    products = pd.DataFrame(prods)

    orders, items = [], []
    item_id = 1
    for i in range(1, n_orders + 1):
        c = customers.iloc[int(rng.integers(0, len(customers)))]
        first_day = max(START, c.signup_date)
        if first_day >= END:
            first_day = START
        od = first_day + timedelta(days=int(rng.integers(0, _days(first_day, END) + 1)))
        status = rng.choice(["completed", "cancelled", "returned"], p=[0.88, 0.07, 0.05])
        amount = 0.0
        for _ in range(int(rng.integers(1, 5))):
            p = products.iloc[int(rng.integers(0, len(products)))]
            qty = int(rng.integers(1, 4))
            disc = float(rng.choice([0.0, 0.05, 0.10], p=[0.7, 0.2, 0.1]))
            line = round(qty * p.unit_price * (1 - disc), 2)
            amount += line
            items.append({"order_item_id": f"I{item_id:06d}", "order_id": f"O{i:05d}",
                          "product_id": p.product_id, "quantity": qty, "unit_price": p.unit_price,
                          "discount": disc})
            item_id += 1
        amount = round(amount, 2)
        refund = 0.0
        if status == "returned":
            refund = amount if rng.random() < 0.7 else round(amount * 0.5, 2)
        orders.append({"order_id": f"O{i:05d}", "customer_id": c.customer_id, "order_date": od,
                       "status": status, "channel": rng.choice(["web", "app", "store"], p=[0.5, 0.3, 0.2]),
                       "amount": amount, "refund_amount": refund})
    orders = pd.DataFrame(orders).sort_values("order_date").reset_index(drop=True)
    orders.loc[orders.index[[100, 2000]], "customer_id"] = ["C9998", "C9999"]   # orphaned customers
    order_items = pd.DataFrame(items)

    for name, df in [("regions", regions), ("customers", customers), ("products", products),
                     ("orders", orders), ("order_items", order_items)]:
        df.to_csv(DATA_DIR / f"{name}.csv", index=False)

    # --- the finance team's monthly report, computed independently of the semantic layer ---
    o = orders[orders.status != "cancelled"].copy()
    o["month"] = pd.to_datetime(o.order_date).dt.to_period("M").dt.to_timestamp().dt.date
    report = (o.groupby("month")
              .agg(orders=("order_id", "nunique"), gross_revenue=("amount", "sum"), refunds=("refund_amount", "sum"))
              .reset_index())
    report["net_revenue"] = (report.gross_revenue - report.refunds).round(2)
    report = report[["month", "orders", "net_revenue"]]
    report.to_csv(REFERENCE_DIR / "finance_monthly_report.csv", index=False)

    counts = {"regions": len(regions), "customers": len(customers), "products": len(products),
              "orders": len(orders), "order_items": len(order_items), "finance_report_months": len(report)}
    print(f"  wrote {counts}")
    return counts
