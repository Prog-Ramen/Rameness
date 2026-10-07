import unittest

from invoicer import Invoice, Line, Money


class TestBasics(unittest.TestCase):
    def test_line_total(self):
        self.assertEqual(Line("A", "Widget", Money.of("2.50"), 4).total(), Money.of("10.00"))

    def test_subtotal_and_tax(self):
        inv = Invoice("INV-1", "Acme", tax_rate=0.1)
        inv.add(Line("A", "Widget", Money.of("10.00"), 2))
        inv.add(Line("B", "Service", Money.of("5.00"), 1, taxable=False))
        self.assertEqual(inv.subtotal(), Money.of("25.00"))
        self.assertEqual(inv.tax(), Money.of("2.00"))
        self.assertEqual(inv.total(), Money.of("27.00"))


if __name__ == "__main__":
    unittest.main()
