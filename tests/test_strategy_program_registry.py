import unittest

from strategy_program_registry import PROGRAMS, create_trader, get_program


class TrustedStrategyRegistryTests(unittest.TestCase):
    def test_four_original_programs_are_registered(self):
        self.assertEqual(len(PROGRAMS), 4)
        self.assertEqual(
            {item["name"] for item in PROGRAMS.values()},
            {
                "JeanLong",
                "JeanShort",
                "PULLBACK LONG EMA50 o 200 D W",
                "PULLBACK SHORT EMA50 o 200 D W",
            },
        )

    def test_momentum_programs_can_be_instantiated(self):
        for program_id in (
            "292a064d-149c-4187-a50a-1d75a3f7a4cd",
            "1d4008e2-73e3-48f1-ad16-cf2a92add3d3",
        ):
            trader = create_trader(program_id, "TEST")
            self.assertEqual(trader.symbol, "TEST")

    def test_pullback_programs_require_explicit_sizing(self):
        for program_id in (
            "19f003ba-03eb-4769-89f3-4ed0fb190110",
            "cd71f2eb-e95a-4b90-981a-2c48560e3323",
        ):
            with self.assertRaisesRegex(ValueError, "pullback_requires_capital_and_daily_pct"):
                create_trader(program_id, "TEST")

    def test_unknown_program_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown_trusted_strategy_program"):
            get_program("arbitrary-database-code")


if __name__ == "__main__":
    unittest.main()
