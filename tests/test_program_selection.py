import unittest

from strategy_store import validate_program_selection


def row(name, enabled=True, strategy_type="python_program"):
    return ("id-" + name, name, "source", strategy_type, enabled, 10.0, None, None)


class ProgramSelectionTests(unittest.TestCase):
    def test_all_four_can_be_saved_when_disabled(self):
        names = (
            "JeanLong",
            "JeanShort",
            "PULLBACK LONG EMA50 o 200 D W",
            "PULLBACK SHORT EMA50 o 200 D W",
        )
        self.assertTrue(validate_program_selection([row(name, False) for name in names]))

    def test_one_enabled_program_is_allowed(self):
        self.assertTrue(validate_program_selection([
            row("JeanLong", True),
            row("JeanShort", False),
        ]))

    def test_two_enabled_programs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "solo una"):
            validate_program_selection([row("JeanLong"), row("JeanShort")])

    def test_formula_strategies_are_not_counted_as_programs(self):
        self.assertTrue(validate_program_selection([
            row("formula-a", True, "formula"),
            row("JeanLong", True),
        ]))


if __name__ == "__main__":
    unittest.main()
