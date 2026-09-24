import json
import unittest
from collections import Counter, defaultdict
from pathlib import Path


def expected():
    customers = json.loads(Path("data/customers.json").read_text())
    orders = [json.loads(p.read_text()) for p in sorted(Path("data/orders").glob("order_*.json"))]
    paid = [o for o in orders if o["status"] == "paid"]
    rev = lambda o: sum(i["qty"] * i["unit_price"] for i in o["items"])
    by_region, by_cust = defaultdict(float), defaultdict(float)
    for o in paid:
        by_region[customers[o["customer"]]["region"]] += rev(o)
        by_cust[o["customer"]] += rev(o)
    return {"total_paid_revenue": round(sum(rev(o) for o in paid), 2),
            "revenue_by_region": {k: round(v, 2) for k, v in by_region.items()},
            "top_customer": max(by_cust, key=by_cust.get),
            "refunded_order_ids": sorted(o["id"] for o in orders if o["status"] == "refunded"),
            "pending_count": sum(o["status"] == "pending" for o in orders),
            "orders_per_customer": dict(Counter(o["customer"] for o in orders))}


class Hidden(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.got = json.loads(Path("report.json").read_text())
        cls.want = expected()

    def test_total(self):
        self.assertAlmostEqual(self.got["total_paid_revenue"], self.want["total_paid_revenue"], places=2)

    def test_regions(self):
        self.assertEqual(set(self.got["revenue_by_region"]), set(self.want["revenue_by_region"]))
        for k, v in self.want["revenue_by_region"].items():
            self.assertAlmostEqual(self.got["revenue_by_region"][k], v, places=2)

    def test_top_customer(self):
        self.assertEqual(self.got["top_customer"], self.want["top_customer"])

    def test_refunded(self):
        self.assertEqual(self.got["refunded_order_ids"], self.want["refunded_order_ids"])

    def test_pending(self):
        self.assertEqual(self.got["pending_count"], self.want["pending_count"])

    def test_orders_per_customer(self):
        self.assertEqual(self.got["orders_per_customer"], self.want["orders_per_customer"])
