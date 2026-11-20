WITH g0 AS (
  SELECT
    'FY' || CAST(YEAR(orders.order_date - INTERVAL '3' MONTH) AS TEXT) || '-' || RIGHT(CAST(YEAR(orders.order_date - INTERVAL '3' MONTH) + 1 AS TEXT), 2) AS order_date_fiscal_year,
    SUM(orders.amount) FILTER(WHERE
      orders.status <> 'cancelled') - SUM(orders.refund_amount) FILTER(WHERE
      orders.status <> 'cancelled') AS net_revenue
  FROM "orders" AS orders
  GROUP BY
    1
), g1 AS (
  SELECT
    'FY' || CAST(YEAR(orders.order_date - INTERVAL '3' MONTH) AS TEXT) || '-' || RIGHT(CAST(YEAR(orders.order_date - INTERVAL '3' MONTH) + 1 AS TEXT), 2) AS order_date_fiscal_year,
    SUM(order_items.quantity) FILTER(WHERE
      orders.status <> 'cancelled') AS units_sold
  FROM "order_items" AS order_items
  LEFT JOIN "orders" AS orders
    ON order_items.order_id = orders.order_id
  GROUP BY
    1
)
SELECT
  COALESCE(g0.order_date_fiscal_year, g1.order_date_fiscal_year) AS order_date_fiscal_year,
  net_revenue,
  units_sold
FROM g0
FULL OUTER JOIN g1
  ON g0.order_date_fiscal_year IS NOT DISTINCT FROM g1.order_date_fiscal_year
ORDER BY
  order_date_fiscal_year ASC
