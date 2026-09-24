import unittest

from inventory import add_item, apply_discount, remove_item, total_value


class T(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add_item({}, "apple", 3), {"apple": 3})

    def test_total(self):
        self.assertEqual(total_value({"apple": 2, "pear": 1}, {"apple": 1.5, "pear": 2.0}), 5.0)

    def test_discount(self):
        self.assertEqual(apply_discount(80.0, 25), 60.0)
