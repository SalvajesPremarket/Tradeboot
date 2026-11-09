import unittest
from paper_trading import _paper_pnl


class PaperAccountingTests(unittest.TestCase):
    def test_long_profit_and_loss(self):
        self.assertAlmostEqual(_paper_pnl("LONG", 10, 11, 5), 5)
        self.assertAlmostEqual(_paper_pnl("LONG", 10, 9, 5), -5)

    def test_short_profit_and_loss(self):
        self.assertAlmostEqual(_paper_pnl("SHORT", 10, 9, 5), 5)
        self.assertAlmostEqual(_paper_pnl("SHORT", 10, 11, 5), -5)

    def test_unknown_side_is_conservatively_treated_as_short(self):
        self.assertAlmostEqual(_paper_pnl("UNKNOWN", 10, 9, 5), 5)


if __name__ == "__main__":
    unittest.main()
