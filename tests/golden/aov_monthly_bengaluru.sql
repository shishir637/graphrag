SELECT
  CAST(DATE_TRUNC('MONTH', orders.order_date) AS DATE) AS order_date_month,
  CAST(SUM(orders.amount) FILTER(WHERE
    orders.status <> 'cancelled') - SUM(orders.refund_amount) FILTER(WHERE
    orders.status <> 'cancelled') AS DOUBLE) / NULLIF(COUNT(DISTINCT orders.order_id) FILTER(WHERE
    orders.status <> 'cancelled'), 0) AS average_order_value
FROM "orders" AS orders
LEFT JOIN "customers" AS customers
  ON orders.customer_id = customers.customer_id
WHERE
  customers.city = 'Bengaluru'
GROUP BY
  1
ORDER BY
  order_date_month ASC
