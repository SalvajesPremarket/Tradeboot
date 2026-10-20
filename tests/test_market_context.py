import unittest

from app import market_values_from_rate_row


class MarketValueMappingTests(unittest.TestCase):
    def test_maps_only_explicit_numeric_quote_fields(self):
        values = market_values_from_rate_row({
            "instrumentID": 123,
            "Price": "12.5",
            "Bid": 12.4,
            "ask": 12.6,
            "changePercent": "3.2",
            "Volume": 15000,
        })
        self.assertEqual(values, {
            "price": 12.5,
            "bid": 12.4,
            "ask": 12.6,
            "change_pct": 3.2,
            "volume": 15000.0,
        })

    def test_does_not_invent_unavailable_values(self):
        values = market_values_from_rate_row({"bid": 10, "ask": None, "volume": "unknown"})
        self.assertEqual(values, {"bid": 10.0})
        self.assertNotIn("rvol", values)
        self.assertNotIn("float_shares", values)
        self.assertNotIn("gap_pct", values)

    def test_ignores_boolean_and_non_finite_values(self):
        values = market_values_from_rate_row({
            "price": True,
            "bid": "NaN",
            "ask": "Infinity",
        })
        self.assertEqual(values, {})


if __name__ == "__main__":
    unittest.main()
