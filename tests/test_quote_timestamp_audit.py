"""Tests for read-only Schwab quote timestamp diagnostics."""
import unittest
from datetime import datetime, timedelta, timezone

from paper_trading import _quote_timestamp_audit


class QuoteTimestampAuditTests(unittest.TestCase):
    def setUp(self):
        self.received_at = datetime(2026, 11, 18, 15, 0, 0, tzinfo=timezone.utc)

    def test_missing_timestamps_are_explicit(self):
        result = _quote_timestamp_audit({}, self.received_at)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "timestamp_missing")
        self.assertEqual(result["fields"], [])

    def test_recent_iso_timestamp_is_accepted_and_normalized(self):
        source_time = self.received_at - timedelta(seconds=4)
        result = _quote_timestamp_audit(
            {"quoteTime": source_time.isoformat()}, self.received_at
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["selected_field"], "quoteTime")
        self.assertEqual(result["selected_timestamp_utc"], source_time.isoformat())
        self.assertEqual(result["selected_age_seconds"], 4.0)

    def test_epoch_milliseconds_are_normalized(self):
        source_time = self.received_at - timedelta(seconds=8)
        epoch_ms = int(source_time.timestamp() * 1000)
        result = _quote_timestamp_audit(
            {"quoteTimeInLong": epoch_ms}, self.received_at
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["selected_field"], "quoteTimeInLong")
        self.assertEqual(result["selected_age_seconds"], 8.0)

    def test_stale_timestamp_is_rejected_with_age(self):
        source_time = self.received_at - timedelta(seconds=45)
        result = _quote_timestamp_audit(
            {"tradeTime": source_time.isoformat()}, self.received_at
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "stale_timestamp")
        self.assertEqual(result["fields"][0]["age_seconds"], 45.0)

    def test_future_timestamp_is_rejected(self):
        source_time = self.received_at + timedelta(seconds=20)
        result = _quote_timestamp_audit(
            {"quoteTime": source_time.isoformat()}, self.received_at
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "future_timestamp")
        self.assertEqual(result["fields"][0]["age_seconds"], -20.0)

    def test_invalid_timestamp_is_not_silently_accepted(self):
        result = _quote_timestamp_audit(
            {"quoteTime": "not-a-timestamp"}, self.received_at
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "timestamp_invalid")
        self.assertEqual(result["fields"][0]["reason"], "invalid_timestamp")

    def test_fresh_alternative_is_selected_when_another_field_is_stale(self):
        stale = self.received_at - timedelta(seconds=60)
        fresh = self.received_at - timedelta(seconds=3)
        result = _quote_timestamp_audit(
            {"quoteTimeInLong": stale.isoformat(), "tradeTime": fresh.isoformat()},
            self.received_at,
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["selected_field"], "tradeTime")
        self.assertEqual(result["selected_age_seconds"], 3.0)
        self.assertEqual(result["fields"][0]["reason"], "stale_timestamp")


if __name__ == "__main__":
    unittest.main()
