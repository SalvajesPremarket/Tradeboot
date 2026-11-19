from datetime import date, datetime, timezone
import unittest
from zoneinfo import ZoneInfo

from paper_trading import _volume_since_previous_close


class PreviousCloseExtendedHoursVolumeTests(unittest.TestCase):
    def setUp(self):
        self.eastern = ZoneInfo("America/New_York")
        self.previous_session = date(2026, 11, 18)
        self.window_end = datetime(2026, 11, 19, 4, 45, tzinfo=self.eastern)

    def row(self, stamp, volume):
        return {"_stamp": stamp, "volume": volume}

    def test_sums_after_hours_and_premarket_and_excludes_before_close_and_open_minute(self):
        rows = [
            self.row(datetime(2026, 11, 18, 15, 59, tzinfo=self.eastern), 9000),
            self.row(datetime(2026, 11, 18, 16, 0, tzinfo=self.eastern), 100),
            self.row(datetime(2026, 11, 18, 19, 59, tzinfo=self.eastern), 50),
            self.row(datetime(2026, 11, 19, 4, 0, tzinfo=self.eastern), 200),
            self.row(datetime(2026, 11, 19, 4, 44, tzinfo=self.eastern), 30),
            self.row(datetime(2026, 11, 19, 4, 45, tzinfo=self.eastern), 700),
        ]
        total, error = _volume_since_previous_close(rows, self.previous_session, self.window_end, self.eastern)
        self.assertIsNone(error)
        self.assertEqual(total, 380)

    def test_zero_volume_is_valid_when_extended_hours_bars_exist(self):
        rows = [self.row(datetime(2026, 11, 18, 16, 0, tzinfo=self.eastern), 0)]
        total, error = _volume_since_previous_close(rows, self.previous_session, self.window_end, self.eastern)
        self.assertIsNone(error)
        self.assertEqual(total, 0)

    def test_missing_extended_hours_history_fails_closed(self):
        total, error = _volume_since_previous_close([], self.previous_session, self.window_end, self.eastern)
        self.assertIsNone(total)
        self.assertEqual(error, "previous_close_intraday_history_missing")

    def test_missing_previous_session_fails_closed(self):
        rows = [self.row(datetime(2026, 11, 19, 4, 0, tzinfo=self.eastern), 10)]
        total, error = _volume_since_previous_close(rows, None, self.window_end, self.eastern)
        self.assertIsNone(total)
        self.assertEqual(error, "previous_close_intraday_history_missing")

    def test_invalid_volume_fails_closed(self):
        rows = [self.row(datetime(2026, 11, 18, 16, 0, tzinfo=self.eastern), float("nan"))]
        total, error = _volume_since_previous_close(rows, self.previous_session, self.window_end, self.eastern)
        self.assertIsNone(total)
        self.assertEqual(error, "previous_close_intraday_volume_invalid")


if __name__ == "__main__":
    unittest.main()
