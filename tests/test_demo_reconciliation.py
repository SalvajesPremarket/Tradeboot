import unittest
from app import reconcile_demo_snapshot

class DemoReconciliationTests(unittest.TestCase):
    def test_candidate_is_never_auto_confirmed(self):
        portfolio = {'data': {'clientPortfolio': {'positions': [{'positionId': 456, 'instrumentId': 123, 'isBuy': True, 'amount': 25.0}]}}}
        executions = [{'idempotency_key': 'test', 'status': 'submitted', 'symbol': 'ABC', 'instrument_id': 123, 'side': 'LONG', 'amount_usd': 25.0}]
        result = reconcile_demo_snapshot(portfolio, executions)
        self.assertEqual(result['items'][0]['reconciliation_status'], 'candidate_match_manual_confirmation')
        self.assertFalse(result['items'][0]['automatic_confirmation'])


    def test_alternate_eToro_portfolio_envelope_and_field_casing(self):
        portfolio = {"data": {"positions": [
            {"PositionId": 456, "InstrumentId": 123, "IsBuy": True, "Amount": 25.0}
        ]}}
        executions = [{"idempotency_key": "test", "status": "submitted", "symbol": "ABC",
                       "instrument_id": 123, "side": "LONG", "amount_usd": 25.0}]
        result = reconcile_demo_snapshot(portfolio, executions)
        self.assertEqual(result["items"][0]["reconciliation_status"], "candidate_match_manual_confirmation")
        self.assertEqual(result["items"][0]["candidate_positions"][0]["position_id"], 456)

    def test_different_amount_does_not_match(self):
        portfolio = {"data": {"clientPortfolio": {"positions": [
            {"positionId": 456, "instrumentId": 123, "isBuy": True, "amount": 50.0}
        ]}}}
        executions = [{"instrument_id": 123, "side": "LONG", "amount_usd": 25.0}]
        result = reconcile_demo_snapshot(portfolio, executions)
        self.assertEqual(result["items"][0]["reconciliation_status"], "no_current_position_match_manual_review")

    def test_missing_position_is_not_rejected(self):
        result = reconcile_demo_snapshot({'data': {'clientPortfolio': {'positions': []}}}, [{'instrument_id': 123, 'side': 'LONG', 'amount_usd': 25}])
        self.assertEqual(result['items'][0]['reconciliation_status'], 'no_current_position_match_manual_review')

    def test_multiple_matches_are_ambiguous(self):
        rows = [{'positionId': n, 'instrumentId': 123, 'isBuy': True, 'amount': 25.0} for n in (456, 457)]
        result = reconcile_demo_snapshot({'data': {'clientPortfolio': {'positions': rows}}}, [{'instrument_id': 123, 'side': 'LONG', 'amount_usd': 25}])
        self.assertEqual(result['items'][0]['reconciliation_status'], 'ambiguous_multiple_positions')

if __name__ == '__main__':
    unittest.main()
