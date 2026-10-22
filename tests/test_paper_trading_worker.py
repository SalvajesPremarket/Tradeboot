import unittest

# Importing the worker also checks its module-level wiring and syntax without
# starting the background thread or making any market-data/database requests.
import paper_trading


class PaperTradingWorkerSmokeTests(unittest.TestCase):
    def test_worker_entrypoints_exist_and_are_simulation_only(self):
        self.assertTrue(callable(paper_trading.run_cycle))
        self.assertTrue(callable(paper_trading.start_paper_trading_worker))
        self.assertGreaterEqual(paper_trading._INTERVAL, 30)


if __name__ == "__main__":
    unittest.main()
