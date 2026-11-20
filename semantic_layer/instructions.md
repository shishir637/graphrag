# Business rules for answering data questions

These rules are given to Claude whenever it turns a question into a semantic-layer request.

- **Currency:** all money is in Indian rupees (INR). Show large numbers in lakh/crore only if the user asks; otherwise use plain numbers with thousands separators.
- **Fiscal year:** April to March. "FY2025-26" means 2025-04-01 to 2026-03-31. "This fiscal year" is the fiscal year containing today's date. Use the `fiscal_year` grain to group by fiscal year.
- **Quarters:** "last quarter" means the last *completed calendar quarter* unless the user says "fiscal quarter" (fiscal Q1 = April-June).
- **Relative dates:** resolve "last month", "this year", "last 90 days" against today's date and send explicit `start_date` / `end_date`. Ranges are inclusive. The current month is incomplete; say so if it is included.
- **Revenue:** "revenue" or "sales" without qualification means `net_revenue` (after refunds, excluding cancelled orders). Use `gross_revenue` only when the user says gross or before refunds.
- **Revenue by product or category:** use `product_revenue` (counted on order lines). `net_revenue` cannot be split by product, because one order holds several products.
- **Cancelled orders** never count in any metric. Returned orders count as orders; their refunds reduce net revenue.
- **Customers:** "customers" in a period means `active_customers`; "new customers" or "signups" means `new_customers`.
- **Data quality:** a few orders refer to customers that no longer exist; they appear with an empty region/city. Mention it if such a row shows up.
