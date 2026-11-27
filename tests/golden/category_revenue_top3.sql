SELECT
  *
FROM (
  SELECT
    products.category AS category,
    SUM(order_items.quantity * order_items.unit_price * (
      1 - order_items.discount
    )) FILTER(WHERE
      orders.status <> 'cancelled') AS product_revenue
  FROM "order_items" AS order_items
  LEFT JOIN "orders" AS orders
    ON order_items.order_id = orders.order_id
  LEFT JOIN "products" AS products
    ON order_items.product_id = products.product_id
  GROUP BY
    1
) AS ranked
WHERE
  NOT category IS NULL
ORDER BY
  product_revenue DESC
LIMIT 3
