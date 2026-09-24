import unittest

from inventory import add_item, apply_discount, remove_item, total_value


class Hidden(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add_item({"a": 1}, "a", 2), {"a": 3})
        with self.assertRaises(ValueError):
            add_item({}, "a", 0)

    def test_remove(self):
        self.assertEqual(remove_item({"a": 3}, "a", 1), {"a": 2})
        self.assertEqual(remove_item({"a": 3}, "a", 3), {})
        with self.assertRaises(ValueError):
            remove_item({"a": 1}, "a", 2)
        with self.assertRaises(KeyError):
            remove_item({}, "zzz", 1)

    def test_total(self):
        self.assertEqual(total_value({"a": 2, "b": 3, "c": 1}, {"a": 1.25, "b": 2.0}), 8.5)
        self.assertEqual(total_value({}, {}), 0)

    def test_discount(self):
        self.assertEqual(apply_discount(80.0, 25), 60.0)
        self.assertEqual(apply_discount(19.99, 0), 19.99)
        self.assertEqual(apply_discount(10.0, 100), 0.0)
        for bad in (-1, 101):
            with self.assertRaises(ValueError):
                apply_discount(10.0, bad)
