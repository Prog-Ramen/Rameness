"""Generates seed/orders.csv (deterministic) and answers.json (the truth) for the data task."""
import csv, json, random
from collections import defaultdict
from datetime import date, timedelta

random.seed(7)
products = {"Widget": 12.5, "Gadget": 49.0, "Gizmo": 7.25, "Doohickey": 120.0, "Sprocket": 3.4}
customers = [f"C{n:03d}" for n in range(1, 61)]
rows, truth_rows = [], []
start = date(2025, 1, 1)
for i in range(1, 401):
    d = start + timedelta(days=random.randrange(0, 365))
    p = random.choice(list(products))
    q = random.randint(1, 6)
    c = random.choice(customers)
    status = random.choices(["paid", "refunded", "paid"], [8, 1, 3])[0]
    fmt = random.choice(["iso", "iso", "dmy", "text"])
    ds = d.isoformat() if fmt == "iso" else d.strftime("%d/%m/%Y") if fmt == "dmy" else d.strftime("%b %d %Y")
    price = products[p]
    ps = random.choice([f"{price:.2f}", f"${price:,.2f}", f"{price:.2f} USD"])
    cust = c if random.random() > 0.03 else ""          # a few missing customers
    rows.append({"order_id": f"O{i:04d}", "date": ds, "customer": cust, "product": p, "quantity": q,
                 "unit_price": ps, "status": status})
    truth_rows.append((f"O{i:04d}", d, cust, p, q, price, status))
# exact duplicate rows (same order exported twice)
for r in random.sample(rows, 12):
    rows.append(dict(r))
random.shuffle(rows)
with open("seed/orders.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0]))
    w.writeheader(); w.writerows(rows)

paid = [t for t in truth_rows if t[6] == "paid"]
revenue = round(sum(t[4] * t[5] for t in paid), 2)
by_prod = defaultdict(float); by_month = defaultdict(float)
for t in paid:
    by_prod[t[3]] += t[4] * t[5]; by_month[t[1].strftime("%Y-%m")] += t[4] * t[5]
answers = {
    "duplicate_rows_removed": 12,
    "paid_orders": len(paid),
    "refunded_orders": sum(1 for t in truth_rows if t[6] == "refunded"),
    "total_paid_revenue": revenue,
    "top_product_by_revenue": max(by_prod, key=by_prod.get),
    "best_month": max(by_month, key=by_month.get),
    "unique_customers": len({t[2] for t in paid if t[2]}),
    "orders_missing_customer": sum(1 for t in truth_rows if not t[2]),
    "average_paid_order_value": round(revenue / len(paid), 2),
}
json.dump(answers, open("answers.json", "w"), indent=1)
print(answers)
