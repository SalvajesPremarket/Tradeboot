"""Unit tests for conservative quote freshness/quality annotations."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import quote_freshness, _parse_provider_bool


def stamp(seconds_ago=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


class ProviderBooleanTests(unittest.TestCase):
    def test_string_false_is_false_not_truthy(self):
        self.assertIs(_parse_provider_bool("False"), False)

    def test_unknown_value_stays_unknown(self):
        self.assertIsNone(_parse_provider_bool("maybe"))


class QuoteFreshnessTests(unittest.TestCase):
    def test_missing_timestamp_is_not_fresh_or_usable(self):
        result = quote_freshness({"rates": [{"bid": 10.0}]})
        self.assertFalse(result["quote_is_fresh"])
        self.assertFalse(result["quote_is_usable"])
        self.assertEqual(result["freshness_status"], "unknown_timestamp")

    def test_recent_timestamp_is_fresh(self):
        result = quote_freshness({"rates": [{"Date": stamp(), "IsMarketOpen": "true"}]})
        self.assertTrue(result["quote_is_fresh"])
        self.assertTrue(result["quote_is_usable"])
        self.assertEqual(result["quote_quality_status"], "fresh_quality_unconfirmed")

    def test_old_timestamp_is_not_usable(self):
        result = quote_freshness({"rates": [{"date": stamp(301), "IsMarketOpen": True}]})
        self.assertFalse(result["quote_is_fresh"])
        self.assertFalse(result["quote_is_usable"])
        self.assertEqual(result["quote_quality_status"], "stale")

    def test_explicitly_closed_market_is_not_usable(self):
        result = quote_freshness({"rates": [{"Date": stamp(), "IsMarketOpen": "False"}]})
        self.assertFalse(result["quote_is_usable"])
        self.assertEqual(result["quote_quality_status"], "market_closed")

    def test_explicit_delayed_quote_is_not_usable(self):
        result = quote_freshness({"rates": [{"Date": stamp(), "IsMarketOpen": True, "isDelayed": True}]})
        self.assertFalse(result["quote_is_usable"])
        self.assertEqual(result["quote_quality_status"], "delayed_quote")

    def test_fresh_open_non_delayed_quote_is_confirmed(self):
        result = quote_freshness({
            "rates": [{
                "Date": stamp(),
                "IsMarketOpen": "True",
                "DelayedAsk": False,
                "DelayedBid": False,
            }]
        })
        self.assertTrue(result["quote_is_fresh"])
        self.assertTrue(result["quote_is_usable"])
        self.assertEqual(result["quote_quality_status"], "fresh_market_open")

    def test_invalid_timestamp_is_not_usable(self):
        result = quote_freshness({"rates": [{"Date": "not-a-timestamp"}]})
        self.assertFalse(result["quote_is_fresh"])
        self.assertFalse(result["quote_is_usable"])
        self.assertEqual(result["freshness_status"], "invalid_timestamp")


if __name__ == "__main__":
    unittest.main()
