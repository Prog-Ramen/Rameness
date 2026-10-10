# TODO: cover the error cases
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mod0 import geo_0_0
from mod0 import geo_0_1
from mod1 import geo_1_0


class Basics(unittest.TestCase):
    def test_0(self):
        self.assertEqual(geo_0_0(2), 2)

    def test_1(self):
        self.assertEqual(geo_0_1(2), 4)

    def test_2(self):
        self.assertEqual(geo_1_0(2), 2)
