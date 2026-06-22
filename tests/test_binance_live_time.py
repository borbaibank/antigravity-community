"""Unit tests for Binance server time offset sync."""

import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import binance_live as bl


class BinanceTimeSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bl._time_offset_ms = 0
        bl._last_time_sync_mono = 0.0

    def tearDown(self):
        bl._time_offset_ms = 0
        bl._last_time_sync_mono = 0.0

    def test_timestamp_ms_applies_offset(self):
        bl._time_offset_ms = 2500
        with patch.object(bl.time, "time", return_value=1000.0):
            self.assertEqual(bl._timestamp_ms(), 1_002_500)

    async def test_sync_server_time_sets_offset_from_server_time(self):
        client = AsyncMock()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"serverTime": 1_700_000_000_000}
        client.get = AsyncMock(return_value=resp)

        with patch.object(bl.time, "time", side_effect=[1000.0, 1000.1]):
            offset = await bl.sync_server_time(client, force=True)

        self.assertEqual(offset, 1_700_000_000_000 - 1_000_050)
        self.assertEqual(bl.time_offset_ms(), offset)
        client.get.assert_awaited_once_with(bl.BASE_URL + "/fapi/v1/time")

    async def test_sync_server_time_skips_within_interval(self):
        client = AsyncMock()
        bl._time_offset_ms = 42
        bl._last_time_sync_mono = time.monotonic()

        offset = await bl.sync_server_time(client, force=False)
        self.assertEqual(offset, 42)
        client.get.assert_not_awaited()

    async def test_sreq_retries_once_on_1021(self):
        client = AsyncMock()
        ok_resp = MagicMock()
        ok_resp.raise_for_status = MagicMock()
        ok_resp.json.return_value = {"ok": True}

        err_body = (
            '{"code":-1021,"msg":"Timestamp for this request is outside of the recvWindow."}'
        )
        clock_err = httpx.HTTPStatusError(
            "400",
            request=httpx.Request("GET", "https://fapi.binance.com/fapi/v2/account"),
            response=httpx.Response(400, text=err_body),
        )
        client.get = AsyncMock(side_effect=[clock_err, ok_resp])

        with patch.object(bl, "sync_server_time", AsyncMock(return_value=0)):
            data = await bl._sreq(client, "GET", "/fapi/v2/account")

        self.assertEqual(data, {"ok": True})
        self.assertEqual(client.get.await_count, 2)

    async def test_sreq_includes_recv_window_and_offset_timestamp(self):
        client = AsyncMock()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {}
        client.get = AsyncMock(return_value=resp)
        bl._time_offset_ms = 100

        with patch.object(bl.time, "time", return_value=2000.0):
            with patch.object(bl, "sync_server_time", AsyncMock(return_value=100)):
                await bl._sreq(client, "GET", "/fapi/v2/account")

        params = client.get.await_args.kwargs["params"]
        self.assertEqual(params["timestamp"], 2_000_100)
        self.assertEqual(params["recvWindow"], bl.RECV_WINDOW)
        self.assertIn("signature", params)


if __name__ == "__main__":
    unittest.main()
