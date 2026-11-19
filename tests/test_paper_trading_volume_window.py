from datetime import datetime, timezone
import unittest

from paper_trading import _volume_window_ticks


class PriorCloseVolumeWindowTests(unittest.TestCase):
    def test_accumulates_after_hours_and_premarket_across_midnight_reset(self):
        baseline = datetime(2026, 11, 18, 21, 0, tzinfo=timezone.utc)
        day_start = datetime(2026, 11, 19, 5, 0, tzinfo=timezone.utc)
        rows = [
            (baseline, 5.00, 2_000_000),
            (datetime(2026, 11, 18, 21, 10, tzinfo=timezone.utc), 5.01, 2_000_300),
            (datetime(2026, 11, 19, 4, 59, tzinfo=timezone.utc), 5.02, 2_001_000),
            (day_start, 5.03, 100),
            (datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc), 5.04, 150),
        ]
        ticks, total, error = _volume_window_ticks(rows, baseline, day_start)
        self.assertIsNone(error)
        self.assertEqual(total, 1_150)
        self.assertEqual([tick['day_volume'] for tick in ticks], [1_100, 1_150])

    def test_missing_close_baseline_fails_closed(self):
        now = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
        ticks, total, error = _volume_window_ticks([], None, now)
        self.assertEqual(ticks, [])
        self.assertIsNone(total)
        self.assertEqual(error, 'previous_close_baseline_missing')


if __name__ == '__main__':
    unittest.main()
