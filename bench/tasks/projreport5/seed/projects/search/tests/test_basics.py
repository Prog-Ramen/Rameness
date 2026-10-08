import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mod0 import search_0_0
from mod0 import search_0_1
from mod1 import search_1_0


class Basics(unittest.TestCase):
    def test_0(self):
        self.assertEqual(search_0_0(2), 3)

    def test_1(self):
        self.assertEqual(search_0_1(2), 4)

    def test_2(self):
        self.assertEqual(search_1_0(2), 2)
