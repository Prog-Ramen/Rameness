orders.csv is an export of our 2025 orders. It is messy: some orders were exported twice (identical rows), dates
come in several formats (ISO 2025-03-05, day-first 05/03/2025, and "Mar 05 2025"), prices may carry "$", commas
or " USD", and some rows have no customer.

Rules: remove exact duplicate rows first. Revenue counts only orders with status "paid" (quantity x unit price).
A month is YYYY-MM of the order date.

Write answers.json with exactly these keys:
- duplicate_rows_removed (int)
- paid_orders (int), refunded_orders (int)
- total_paid_revenue (number, 2 decimals)
- top_product_by_revenue (product name, paid orders only)
- best_month (YYYY-MM with the highest paid revenue)
- unique_customers (distinct customers among paid orders, ignoring missing ones)
- orders_missing_customer (orders with no customer, after removing duplicates)
- average_paid_order_value (total_paid_revenue / paid_orders, 2 decimals)
