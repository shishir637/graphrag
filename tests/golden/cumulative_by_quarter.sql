SELECT
  CAST(DATE_TRUNC('QUARTER', orders.order_date) AS DATE) AS order_date_quarter,
  SUM(
    SUM(orders.amount) FILTER(WHERE
      orders.status <> 'cancelled') - SUM(orders.refund_amount) FILTER(WHERE
      orders.status <> 'cancelled')
  ) OVER (ORDER BY CAST(DATE_TRUNC('QUARTER', orders.order_date) AS DATE)) AS cumulative_net_revenue
FROM "orders" AS orders
GROUP BY
  1
ORDER BY
  order_date_quarter ASC
