SELECT
  regions.region_name AS region_name,
  SUM(orders.amount) FILTER(WHERE
    orders.status <> 'cancelled') - SUM(orders.refund_amount) FILTER(WHERE
    orders.status <> 'cancelled') AS net_revenue,
  COUNT(DISTINCT orders.order_id) FILTER(WHERE
    orders.status <> 'cancelled') AS order_count
FROM "orders" AS orders
LEFT JOIN "customers" AS customers
  ON orders.customer_id = customers.customer_id
LEFT JOIN "regions" AS regions
  ON customers.region_id = regions.region_id
WHERE
  orders.order_date >= CAST('2025-04-01' AS DATE)
  AND orders.order_date <= CAST('2026-03-31' AS DATE)
GROUP BY
  1
ORDER BY
  region_name ASC
