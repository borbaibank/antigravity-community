"""Unit tests for Pionex signed GET requests."""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pionex_live as pl


class PionexSignedGetTests(unittest.TestCase):
    def test_build_signed_get_message_and_headers(self):
        with patch.object(pl, "PIONEX_API_SECRET", "test_secret"), patch.object(
            pl, "PIONEX_API_KEY", "test_key"
        ), patch.object(pl, "_timestamp_ms", return_value=1566691672311):
            url, params, headers = pl._build_signed_get(
                "/api/v1/wallet/balancesFull",
            )

        self.assertEqual(
            url,
            "https://api.pionex.com/api/v1/wallet/balancesFull?timestamp=1566691672311",
        )
        self.assertEqual(params["timestamp"], 1566691672311)
        self.assertEqual(headers["PIONEX-KEY"], "test_key")
        self.assertRegex(headers["PIONEX-SIGNATURE"], r"^[0-9a-f]{64}$")

        expected_message = (
            "GET/api/v1/wallet/balancesFull?timestamp=1566691672311"
        )
        import hashlib
        import hmac

        expected_sig = hmac.new(
            b"test_secret", expected_message.encode(), hashlib.sha256
        ).hexdigest()
        self.assertEqual(headers["PIONEX-SIGNATURE"], expected_sig)

    def test_build_signed_get_sorts_params(self):
        with patch.object(pl, "PIONEX_API_SECRET", "secret"), patch.object(
            pl, "PIONEX_API_KEY", "key"
        ), patch.object(pl, "_timestamp_ms", return_value=1000):
            url, _, _ = pl._build_signed_get(
                "/api/v1/wallet/balancesFull",
                {"appLang": "en", "sysLang": "zh"},
            )

        self.assertIn("appLang=en", url)
        self.assertIn("sysLang=zh", url)
        self.assertIn("timestamp=1000", url)
        self.assertLess(url.index("appLang"), url.index("sysLang"))
        self.assertLess(url.index("sysLang"), url.index("timestamp"))


class PionexThbRateTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_usdt_thb_rate_uses_binance_th(self):
        client = AsyncMock()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"symbol": "USDTTHB", "price": "32.46"}
        client.get = AsyncMock(return_value=resp)

        with patch.object(pl, "PIONEX_USDT_THB_RATE", 0):
            rate = await pl.fetch_usdt_thb_rate(client)

        self.assertEqual(rate, 32.46)
        client.get.assert_awaited_once()
        self.assertIn("binance.th", client.get.await_args.args[0])

    async def test_fetch_usdt_thb_rate_env_override(self):
        client = AsyncMock()
        with patch.object(pl, "PIONEX_USDT_THB_RATE", 33.5):
            rate = await pl.fetch_usdt_thb_rate(client)
        self.assertEqual(rate, 33.5)
        client.get.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
