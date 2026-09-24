The `data/` directory holds 60 order files (`data/orders/order_*.json`) and `data/customers.json`
(customer id -> name and region). Read the data and write `report.json` in the current directory with
exactly these keys:

- `total_paid_revenue`: sum of qty * unit_price over all items of orders with status "paid", rounded to 2 decimals
- `revenue_by_region`: object mapping each region to its paid revenue (rounded to 2 decimals); include only
  regions that have paid orders
- `top_customer`: the customer id with the highest paid revenue
- `refunded_order_ids`: list of ids of orders with status "refunded", sorted ascending
- `pending_count`: number of orders with status "pending"
- `orders_per_customer`: object mapping every customer id that appears in any order to its number of orders
  (any status)
