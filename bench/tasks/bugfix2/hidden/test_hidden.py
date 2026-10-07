"""Hidden tests for bugfix2: six subtle issues plus behaviour that must not regress."""
import csv
import io
import unittest
from datetime import date

from invoicer import Invoice, Line, Money, parse_date, to_csv


def rows(inv):
    return list(csv.reader(io.StringIO(to_csv(inv))))


class Issues(unittest.TestCase):
    def test_1_discount_share_exact(self):
        inv = Invoice("I", "C", discount_percent=15, tax_rate=0.2)
        inv.add(Line("A", "t", Money.of("1.50"))); inv.add(Line("B", "u", Money.of("2.70"), taxable=False))
        self.assertEqual(inv.taxable_amount(), Money.of("1.28"))
        inv2 = Invoice("I", "C", discount_percent=33)
        inv2.add(Line("A", "t", Money.of("1.29"))); inv2.add(Line("B", "u", Money.of("3.87"), taxable=False))
        self.assertEqual(inv2.taxable_amount(), Money.of("0.87"))

    def test_2_merge_only_same_price(self):
        inv = Invoice("I", "C")
        inv.add(Line("A", "w", Money.of("1.00"), 2)); inv.add(Line("A", "w", Money.of("1.20"), 3))
        self.assertEqual(len(inv.lines), 2)
        self.assertEqual(inv.subtotal(), Money.of("5.60"))
        inv.add(Line("A", "w", Money.of("1.00"), 1))
        self.assertEqual([l.quantity for l in inv.lines], [3, 3])

    def test_3_dotted_dates(self):
        self.assertEqual(parse_date("05.03.2024"), date(2024, 3, 5))

    def test_4_impossible_date_message(self):
        with self.assertRaisesRegex(ValueError, r"31/02/2024"):
            parse_date("31/02/2024")

    def test_5_csv_formula_neutralised(self):
        inv = Invoice("I", "C")
        for i, d in enumerate(["=SUM(A1:A9)", "+1", "-2", "@cmd", "safe = fine"]):
            inv.add(Line(f"S{i}", d, Money.of("1.00")))
        got = [r[1] for r in rows(inv)[1:6]]
        self.assertEqual(got, ["'=SUM(A1:A9)", "'+1", "'-2", "'@cmd", "safe = fine"])

    def test_6_quantity_rules(self):
        with self.assertRaises(ValueError):
            Invoice("I", "C").add(Line("A", "w", Money.of("1.00"), 0))
        with self.assertRaises(ValueError):
            Invoice("I", "C").add(Line("A", "w", Money.of("1.00"), -2))
        cn = Invoice("CN", "C", credit_note=True).add(Line("A", "w", Money.of("1.00"), -2))
        self.assertEqual(cn.subtotal(), Money.of("-2.00"))


class NoRegressions(unittest.TestCase):
    def test_half_up_and_str(self):
        self.assertEqual(str(Money.of("12.345")), "12.35")
        self.assertEqual(str(Money.of("1.00") - Money.of("2.50")), "-1.50")

    def test_same_sku_same_price_merges(self):
        inv = Invoice("I", "C")
        inv.add(Line("A", "w", Money.of("1.00"), 2)); inv.add(Line("A", "w", Money.of("1.00"), 3))
        self.assertEqual(len(inv.lines), 1); self.assertEqual(inv.lines[0].quantity, 5)

    def test_existing_dates(self):
        self.assertEqual(parse_date("2024-03-05"), date(2024, 3, 5))
        self.assertEqual(parse_date("05/03/2024"), date(2024, 3, 5))

    def test_csv_commas_kept(self):
        inv = Invoice("I", "C").add(Line("B", "Bolt, M6, zinc", Money.of("0.20"), 10))
        self.assertEqual(rows(inv)[1][1], "Bolt, M6, zinc")

    def test_discount_range(self):
        with self.assertRaises(ValueError):
            Invoice("I", "C", discount_percent=101)

    def test_totals_simple(self):
        inv = Invoice("I", "C", discount_percent=10, tax_rate=0.2)
        inv.add(Line("A", "T", Money.of("100.00"))); inv.add(Line("B", "U", Money.of("100.00"), taxable=False))
        self.assertEqual(inv.total(), Money.of("198.00"))


if __name__ == "__main__":
    unittest.main()
