"""Hidden tests for the bugfix task: the five issues, plus behaviour that must not regress."""
import csv
import io
import unittest
from datetime import date

from invoicer import Invoice, Line, Money, parse_date, to_csv


class Issues(unittest.TestCase):
    def test_1_rounds_half_up(self):
        self.assertEqual(str(Money.of("12.345")), "12.35")
        self.assertEqual(str(Money.of(0.125)), "0.13")

    def test_2_same_sku_adds_quantity(self):
        inv = Invoice("I", "C")
        inv.add(Line("A", "Widget", Money.of("1.00"), 2))
        inv.add(Line("A", "Widget", Money.of("1.00"), 3))
        self.assertEqual(len(inv.lines), 1)
        self.assertEqual(inv.lines[0].quantity, 5)
        self.assertEqual(inv.subtotal(), Money.of("5.00"))

    def test_3_day_first_dates(self):
        self.assertEqual(parse_date("05/03/2024"), date(2024, 3, 5))
        self.assertEqual(parse_date("31/12/2023"), date(2023, 12, 31))

    def test_4_csv_keeps_commas(self):
        inv = Invoice("I", "C").add(Line("B1", "Bolt, M6, zinc", Money.of("0.20"), 10))
        rows = list(csv.reader(io.StringIO(to_csv(inv))))
        self.assertEqual(rows[1][1], "Bolt, M6, zinc")

    def test_5_discount_out_of_range_raises(self):
        with self.assertRaises(ValueError):
            Invoice("I", "C", discount_percent=150)
        with self.assertRaises(ValueError):
            Invoice("I", "C", discount_percent=-5)


class NoRegressions(unittest.TestCase):
    def test_money_arithmetic_and_str(self):
        self.assertEqual(str(Money.of("3.10") + Money.of("0.90")), "4.00")
        self.assertEqual(str(Money.of("1.00") - Money.of("2.50")), "-1.50")
        self.assertEqual(Money.of("2.00").times(3), Money.of("6.00"))

    def test_money_rejects_floats_in_constructor(self):
        with self.assertRaises(TypeError):
            Money(1.5)

    def test_iso_dates(self):
        self.assertEqual(parse_date("2024-03-05"), date(2024, 3, 5))
        self.assertEqual(parse_date(" 2023-12-31 "), date(2023, 12, 31))

    def test_different_skus_stay_separate(self):
        inv = Invoice("I", "C")
        inv.add(Line("A", "x", Money.of("1.00"), 1))
        inv.add(Line("B", "y", Money.of("2.00"), 1))
        self.assertEqual(len(inv.lines), 2)

    def test_discount_and_tax(self):
        inv = Invoice("I", "C", discount_percent=10, tax_rate=0.2)
        inv.add(Line("A", "Taxed", Money.of("100.00"), 1))
        inv.add(Line("B", "Untaxed", Money.of("100.00"), 1, taxable=False))
        self.assertEqual(inv.discount(), Money.of("20.00"))
        self.assertEqual(inv.tax(), Money.of("18.00"))
        self.assertEqual(inv.total(), Money.of("198.00"))

    def test_valid_discounts_accepted(self):
        Invoice("I", "C", discount_percent=0)
        Invoice("I", "C", discount_percent=100)

    def test_csv_summary_rows(self):
        inv = Invoice("I", "C", tax_rate=0.1).add(Line("A", "W", Money.of("10.00"), 1))
        rows = list(csv.reader(io.StringIO(to_csv(inv))))
        self.assertEqual(rows[0], ["sku", "description", "quantity", "unit_price", "line_total"])
        self.assertEqual(rows[-1], ["", "total", "", "", "11.00"])


if __name__ == "__main__":
    unittest.main()
