import os
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

from app import app
from etoro_client import EtoroClient, EtoroError


class TradebootApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_health_identifies_etoro_only(self):
        response = self.client.get('/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['broker'], 'eToro')

    def test_status_does_not_expose_credentials(self):
        with patch.dict(os.environ, {'ETORO_API_KEY': 'public-secret-test', 'ETORO_USER_KEY': 'user-secret-test'}):
            response = self.client.get('/api/status')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['credentials_configured'])
        self.assertNotIn('public-secret-test', response.text)
        self.assertNotIn('user-secret-test', response.text)
        self.assertFalse(response.json()['trading_enabled'])

    def test_connection_requires_credentials(self):
        with patch.dict(os.environ, {'ETORO_API_KEY': '', 'ETORO_USER_KEY': ''}, clear=False):
            response = self.client.get('/api/etoro/connection')
        self.assertEqual(response.status_code, 503)

    def test_client_requires_both_keys(self):
        client = EtoroClient(api_key='only-one', user_key='')
        self.assertFalse(client.configured)
        with self.assertRaises(EtoroError):
            client.get_watchlist()

    def test_environment_is_validated(self):
        with self.assertRaises(ValueError):
            EtoroClient(api_key='a', user_key='b', environment='schwab')


if __name__ == '__main__':
    unittest.main()
