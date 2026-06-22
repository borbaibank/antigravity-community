import asyncio
import copy
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import server
from bot.engine.history import _max_drawdown_from_equity_values


class HedgeOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.orig_state = copy.deepcopy(server.state)
        self.orig_live_mode = server.LIVE_MODE
        self.orig_binance_testnet = server.BINANCE_TESTNET
        self.orig_local_sltp = server.LOCAL_SLTP
        self.orig_sltp_mode = server.SLTP_MODE
        self.orig_http_client = server._http_client
        self.orig_exchange_account = copy.deepcopy(server.exchange_account)
        self.orig_latest_prices = copy.deepcopy(server.latest_prices)
        self.orig_circuit_breaker = server._circuit_breaker
        self.orig_last_price_ws_ok_at = server._last_price_ws_ok_at
        self.orig_last_scheduler_ok_at = server._last_scheduler_ok_at
        self.orig_last_watchdog_ok_at = server._last_watchdog_ok_at
        self.orig_last_sync_ok_at = server._last_sync_ok_at
        self.orig_last_exchange_account_ok_at = server._last_exchange_account_ok_at
        self.orig_recent_bot_close_fills = copy.deepcopy(server._recent_bot_close_fills)

        server._http_client = object()
        server.BINANCE_TESTNET = False
        server.LOCAL_SLTP = False
        server.SLTP_MODE = "binance"
        server.exchange_account = {"availableBalance": 10_000.0}
        server.state = {
            "balances": {
                "TabA": 10_000.0,
                "TabB": 10_000.0,
                "Tab1": 10_000.0,
                "SafeGuard": 0.0,
                "Recovered": 0.0,
            },
            "unrealized_pnls": {
                "TabA": 0.0,
                "TabB": 0.0,
                "SafeGuard": 0.0,
                "Recovered": 0.0,
            },
            "open_positions": {},
            "position_registry": {},
            "history": [],
            "used_setups": [],
            "sync_issues": [],
            "error_events": [],
            "pending_entry_orders": {},
        }
        server._recent_bot_close_fills.clear()

    def tearDown(self):
        server.state = self.orig_state
        server.LIVE_MODE = self.orig_live_mode
        server.BINANCE_TESTNET = self.orig_binance_testnet
        server.LOCAL_SLTP = self.orig_local_sltp
        server.SLTP_MODE = self.orig_sltp_mode
        server._http_client = self.orig_http_client
        server.exchange_account = self.orig_exchange_account
        server.latest_prices = self.orig_latest_prices
        server._circuit_breaker = self.orig_circuit_breaker
        server._last_price_ws_ok_at = self.orig_last_price_ws_ok_at
        server._last_scheduler_ok_at = self.orig_last_scheduler_ok_at
        server._last_watchdog_ok_at = self.orig_last_watchdog_ok_at
        server._last_sync_ok_at = self.orig_last_sync_ok_at
        server._last_exchange_account_ok_at = self.orig_last_exchange_account_ok_at
        server._recent_bot_close_fills = self.orig_recent_bot_close_fills

    @staticmethod
    def _long_pos():
        return {
            "tab": "TabA",
            "symbol": "ETHUSDT",
            "side": "Long",
            "position_side": "LONG",
            "entry_price": 3000.0,
            "sl": 2940.0,
            "tp": 3120.0,
            "qty": 0.5,
            "entry_time": "2026-04-17T10:00:00",
            "sl_order_id": 101,
            "tp_order_id": 102,
        }

    @staticmethod
    def _short_pos():
        return {
            "tab": "TabB",
            "symbol": "ETHUSDT",
            "side": "Short",
            "position_side": "SHORT",
            "entry_price": 3010.0,
            "sl": 3070.0,
            "tp": 2890.0,
            "qty": 0.5,
            "entry_time": "2026-04-17T10:05:00",
            "sl_order_id": 201,
            "tp_order_id": 202,
        }

    async def test_manual_close_only_cancels_matching_position_side(self):
        server.LIVE_MODE = True
        server.state["open_positions"] = {
            "ETHUSDT_TabA": self._long_pos(),
            "ETHUSDT_TabB": self._short_pos(),
        }

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "_reconcile_pnl_from_binance", AsyncMock()),
            patch.object(server, "trigger_income_sync", AsyncMock()),
            patch.object(server, "_sweep_algo_orders_for", AsyncMock()),
            patch.object(server.binance_live, "place_market_order", AsyncMock(return_value={"orderId": 999})),
            patch.object(server.binance_live, "cancel_algo_order", AsyncMock()) as cancel_algo,
        ):
            await server._close_position_unsafe("ETHUSDT_TabA", 3050.0, "Manual")

        self.assertNotIn("ETHUSDT_TabA", server.state["open_positions"])
        self.assertIn("ETHUSDT_TabB", server.state["open_positions"])
        self.assertEqual(cancel_algo.await_count, 2)
        cancel_algo.assert_any_await(server._http_client, algo_id=101)
        cancel_algo.assert_any_await(server._http_client, algo_id=102)

    async def test_testnet_close_falls_back_to_price_match_on_percent_filter(self):
        server.LIVE_MODE = True
        server.BINANCE_TESTNET = True
        server.state["open_positions"] = {"ETHUSDT_TabA": self._long_pos()}
        request = httpx.Request("POST", "https://testnet.binancefuture.com/fapi/v1/order")
        response = httpx.Response(
            400,
            request=request,
            text='{"code":-4131,"msg":"The counterparty best price does not meet the PERCENT_PRICE filter."}',
        )

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "_reconcile_pnl_from_binance", AsyncMock()),
            patch.object(server, "trigger_income_sync", AsyncMock()),
            patch.object(server, "_sweep_algo_orders_for", AsyncMock()),
            patch.object(server.asyncio, "sleep", AsyncMock()),
            patch.object(
                server.binance_live,
                "place_market_order",
                AsyncMock(side_effect=httpx.HTTPStatusError("percent", request=request, response=response)),
            ) as place_market,
            patch.object(
                server.binance_live,
                "place_price_match_ioc_order",
                AsyncMock(return_value={"orderId": 1001}),
            ) as place_ioc,
            patch.object(server.binance_live, "cancel_algo_order", AsyncMock()) as cancel_algo,
        ):
            await server._close_position_unsafe("ETHUSDT_TabA", 3050.0, "Manual")

        place_market.assert_awaited_once()
        place_ioc.assert_awaited_once()
        self.assertNotIn("ETHUSDT_TabA", server.state["open_positions"])
        self.assertEqual(cancel_algo.await_count, 2)

    async def test_handle_order_update_closes_only_matching_short_leg(self):
        server.LIVE_MODE = True
        server.state["open_positions"] = {
            "ETHUSDT_TabA": self._long_pos(),
            "ETHUSDT_TabB": self._short_pos(),
        }

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "_reconcile_pnl_from_binance", AsyncMock()),
            patch.object(server, "trigger_income_sync", AsyncMock()),
            patch.object(server, "_sweep_algo_orders_for", AsyncMock()),
            patch.object(server.binance_live, "cancel_algo_order", AsyncMock()) as cancel_algo_order,
        ):
            await server.handle_order_update(
                {
                    "X": "FILLED",
                    "i": 202,
                    "s": "ETHUSDT",
                    "ps": "SHORT",
                    "S": "BUY",
                    "o": "TAKE_PROFIT_MARKET",
                    "ap": "2890.0",
                }
            )

        self.assertIn("ETHUSDT_TabA", server.state["open_positions"])
        self.assertNotIn("ETHUSDT_TabB", server.state["open_positions"])
        cancel_algo_order.assert_awaited_once_with(server._http_client, algo_id=201)

    def test_vanished_exchange_protection_reason_hybrid_sl(self):
        pos = {
            "sl_order_id": 501,
            "tp_order_id": None,
            "sl_source": "exchange",
            "tp_source": "local",
        }
        self.assertEqual(server._vanished_exchange_protection_reason(pos, {502}), "SL")
        self.assertIsNone(server._vanished_exchange_protection_reason(pos, {501, 502}))

    def test_vanished_exchange_protection_reason_hybrid_tp(self):
        pos = {
            "sl_order_id": 501,
            "tp_order_id": 502,
            "sl_source": "local",
            "tp_source": "exchange",
        }
        self.assertEqual(server._vanished_exchange_protection_reason(pos, {501}), "TP")
        self.assertIsNone(server._vanished_exchange_protection_reason(pos, {501, 502}))

    async def test_handle_order_update_exchange_algo_sl_market_fill_infers_sl(self):
        """Exchange CONDITIONAL SL triggers as MARKET on UDS — must label SL + Telegram."""
        server.LIVE_MODE = True
        server.state["balances"]["Tab11"] = 10_000.0
        server.state["open_positions"] = {
            "PORTALUSDT_Tab11": {
                "tab": "Tab11",
                "symbol": "PORTALUSDT",
                "side": "Short",
                "position_side": "SHORT",
                "entry_price": 0.01568,
                "sl": 0.01678,
                "tp": 0.01431,
                "qty": 637.8,
                "entry_time": "2026-06-07T10:00:08",
                "sl_order_id": 9001,
                "tp_order_id": None,
                "sl_source": "exchange",
                "tp_source": "local",
                "placed_sl": 0.01678,
                "placed_tp": 0.01431,
            },
        }
        send_tg = AsyncMock()

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", send_tg),
            patch.object(server, "_reconcile_pnl_from_binance", AsyncMock(return_value={
                "net_pnl": -0.69,
                "exit_price": 0.01672,
                "fee_usd": 0.005,
            })),
            patch.object(server, "trigger_income_sync", AsyncMock()),
            patch.object(server, "_sweep_algo_orders_for", AsyncMock()),
            patch.object(server.binance_live, "cancel_algo_order", AsyncMock()),
            patch.object(
                server.binance_live,
                "fetch_open_algo_order_ids",
                AsyncMock(return_value=set()),
            ),
        ):
            await server.handle_order_update(
                {
                    "X": "FILLED",
                    "i": 88001,
                    "s": "PORTALUSDT",
                    "ps": "SHORT",
                    "S": "BUY",
                    "o": "MARKET",
                    "R": True,
                    "q": "637.8",
                    "ap": "0.01672",
                    "rp": "-0.695",
                }
            )
            await asyncio.sleep(0)

        self.assertNotIn("PORTALUSDT_Tab11", server.state["open_positions"])
        self.assertEqual(server.state["history"][-1]["reason"], "SL")
        send_tg.assert_awaited()
        self.assertEqual(send_tg.await_args.kwargs.get("exit_reason"), "SL")

    async def test_handle_order_update_true_manual_close_stays_manual(self):
        """Market close with exchange SL algo still open stays ManualClose (no Telegram)."""
        server.LIVE_MODE = True
        server.state["balances"]["Tab11"] = 10_000.0
        server.state["open_positions"] = {
            "PORTALUSDT_Tab11": {
                "tab": "Tab11",
                "symbol": "PORTALUSDT",
                "side": "Short",
                "position_side": "SHORT",
                "entry_price": 0.01568,
                "sl": 0.01678,
                "tp": 0.01431,
                "qty": 637.8,
                "entry_time": "2026-06-07T10:00:08",
                "sl_order_id": 9001,
                "sl_source": "exchange",
                "tp_source": "local",
            },
        }
        send_tg = AsyncMock()

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", send_tg),
            patch.object(server, "_reconcile_pnl_from_binance", AsyncMock()),
            patch.object(server, "trigger_income_sync", AsyncMock()),
            patch.object(server, "_sweep_algo_orders_for", AsyncMock()),
            patch.object(server.binance_live, "cancel_algo_order", AsyncMock()),
            patch.object(
                server.binance_live,
                "fetch_open_algo_order_ids",
                AsyncMock(return_value={9001}),
            ),
        ):
            await server.handle_order_update(
                {
                    "X": "FILLED",
                    "i": 88002,
                    "s": "PORTALUSDT",
                    "ps": "SHORT",
                    "S": "BUY",
                    "o": "MARKET",
                    "R": True,
                    "q": "637.8",
                    "ap": "0.01672",
                }
            )
            await asyncio.sleep(0)

        self.assertEqual(server.state["history"][-1]["reason"], "ManualClose")
        send_tg.assert_not_awaited()

    async def test_manual_close_qty_match_closes_matching_tab(self):
        """Manual close with a unique exact qty match must not defer to live sync."""
        server.LIVE_MODE = True
        server.state["balances"].update({"Tab3": 10_000.0, "Tab11": 10_000.0})
        server.state["open_positions"] = {
            "DEXEUSDT_Tab3": {
                "tab": "Tab3",
                "symbol": "DEXEUSDT",
                "side": "Long",
                "position_side": "LONG",
                "entry_price": 10.0,
                "sl": 9.0,
                "tp": 11.0,
                "qty": 50.0,
                "entry_time": "2026-06-09T08:00:00",
                "sl_order_id": 1001,
                "tp_order_id": 1002,
            },
            "DEXEUSDT_Tab11": {
                "tab": "Tab11",
                "symbol": "DEXEUSDT",
                "side": "Long",
                "position_side": "LONG",
                "entry_price": 10.1,
                "sl": 9.1,
                "tp": 11.1,
                "qty": 30.0,
                "entry_time": "2026-06-09T09:00:00",
                "sl_order_id": 2001,
                "tp_order_id": 2002,
            },
        }
        record_issue = AsyncMock()
        sync = AsyncMock()

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "_reconcile_pnl_from_binance", AsyncMock()),
            patch.object(server, "trigger_income_sync", AsyncMock()),
            patch.object(server, "_sweep_algo_orders_for", AsyncMock()),
            patch.object(server.binance_live, "cancel_algo_order", AsyncMock()),
            patch.object(
                server.binance_live,
                "fetch_open_algo_order_ids",
                AsyncMock(return_value={1001, 1002, 2001, 2002}),
            ),
            patch.object(server, "record_sync_issue", record_issue),
            patch.object(server, "sync_live_positions", sync),
        ):
            await server.handle_order_update(
                {
                    "X": "FILLED",
                    "i": 9999,
                    "s": "DEXEUSDT",
                    "ps": "LONG",
                    "S": "SELL",
                    "o": "MARKET",
                    "R": True,
                    "q": "30",
                    "ap": "10.05",
                }
            )
            await asyncio.sleep(0)

        self.assertNotIn("DEXEUSDT_Tab11", server.state["open_positions"])
        self.assertIn("DEXEUSDT_Tab3", server.state["open_positions"])
        record_issue.assert_not_awaited()
        sync.assert_not_awaited()

    async def test_record_sync_issue_dedupes_recent_duplicate(self):
        server.state["sync_issues"] = [{
            "message": "test duplicate",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }]
        with patch.object(server, "save_state", AsyncMock()) as save:
            await server.record_sync_issue("test duplicate")
        self.assertEqual(len(server.state["sync_issues"]), 1)
        save.assert_not_awaited()

    def test_prune_sync_issues_drops_resolved_ambiguous_manual_close(self):
        server.state["sync_issues"] = [
            {
                "message": "Ambiguous manual close fill on DEXEUSDT (LONG) — deferred to live sync",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            {
                "message": "Unrelated issue",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        ]
        changed = server._prune_sync_issues(["DEXEUSDT_Tab11"])
        self.assertTrue(changed)
        self.assertEqual(len(server.state["sync_issues"]), 1)
        self.assertIn("Unrelated", server.state["sync_issues"][0]["message"])

    def test_health_stale_thresholds_align_with_uds_sync_interval(self):
        server.LIVE_MODE = True
        server._uds_connected = True
        import time as _time_mod
        server._last_uds_account_update_mono = _time_mod.monotonic()
        account_max, sync_max = server._health_stale_thresholds()
        self.assertGreaterEqual(account_max, 90)
        self.assertGreaterEqual(sync_max, 180)

    def test_price_tick_monitor_interval_faster_for_hybrid_positions(self):
        server.LIVE_MODE = True
        server.state["open_positions"] = {
            "BTCUSDT_Tab11": {
                "sl_source": "exchange",
                "tp_source": "local",
            },
        }
        self.assertEqual(server._price_tick_monitor_interval_sec(), 0.5)
        server.state["open_positions"] = {
            "BTCUSDT_Tab11": {
                "sl_source": "exchange",
                "tp_source": "exchange",
            },
        }
        self.assertEqual(server._price_tick_monitor_interval_sec(), 1.0)

    async def test_purge_cancels_untracked_local_policy_algo_on_live_position(self):
        server.LIVE_MODE = True
        server.BINANCE_TESTNET = True
        server.state["open_positions"] = {
            "ONUSDT_Tab12": {
                "tab": "Tab12",
                "symbol": "ONUSDT",
                "side": "Short",
                "position_side": "SHORT",
                "entry_price": 0.1306,
                "sl": 0.1388,
                "tp": 0.1184,
                "qty": 765.0,
                "entry_time": "2026-04-27T17:01:15",
                "protection_mode": "local",
                "protection_status": "local",
            }
        }

        async def fake_sreq(_client, _method, path, params=None):
            if path == "/fapi/v1/openAlgoOrders":
                return [
                    {
                        "symbol": "ONUSDT",
                        "algoId": 501,
                        "orderType": "STOP_MARKET",
                        "positionSide": "SHORT",
                        "quantity": "765",
                        "triggerPrice": "0.1388",
                        "clientAlgoId": "AG_Tab12_S_SL_260427102552378",
                    },
                    {
                        "symbol": "ONUSDT",
                        "algoId": 502,
                        "orderType": "TAKE_PROFIT_MARKET",
                        "positionSide": "SHORT",
                        "quantity": "765",
                        "triggerPrice": "0.1184",
                        "clientAlgoId": "AG_Tab12_S_TP_260427102552501",
                    },
                ]
            return {}

        with (
            patch.object(server, "record_sync_issue", AsyncMock()) as record_issue,
            patch.object(
                server.binance_live,
                "get_position_risk",
                AsyncMock(return_value=[
                    {
                        "symbol": "ONUSDT",
                        "positionAmt": "-765",
                        "positionSide": "SHORT",
                        "entryPrice": "0.1306",
                        "markPrice": "0.1200",
                    }
                ]),
            ),
            patch.object(server.binance_live, "_sreq", AsyncMock(side_effect=fake_sreq)),
            patch.object(server.binance_live, "cancel_algo_order", AsyncMock()) as cancel_algo,
        ):
            await server.purge_orphaned_algo_orders()

        self.assertEqual(cancel_algo.await_count, 2)
        cancel_algo.assert_any_await(server._http_client, algo_id=501)
        cancel_algo.assert_any_await(server._http_client, algo_id=502)
        record_issue.assert_not_awaited()

    async def test_bot_close_echo_does_not_qty_fallback_to_sibling_strategy(self):
        server.LIVE_MODE = True
        server.state["balances"].update({"Tab7": 10_000.0})
        server.state["open_positions"] = {
            "HEMIUSDT_LONG_Recovered": {
                "tab": "SafeGuard",
                "symbol": "HEMIUSDT",
                "side": "Long",
                "position_side": "LONG",
                "entry_price": 0.0093,
                "sl": 0.0091,
                "tp": 0.0098,
                "qty": 10784.0,
                "entry_time": "2026-04-27T01:06:00",
                "sl_order_id": 901,
                "tp_order_id": 902,
            },
            "HEMIUSDT_Tab7": {
                "tab": "Tab7",
                "symbol": "HEMIUSDT",
                "side": "Long",
                "position_side": "LONG",
                "entry_price": 0.0093,
                "sl": 0.0088,
                "tp": 0.0101,
                "qty": 10784.0,
                "entry_time": "2026-04-26T13:06:00",
                "sl_order_id": 701,
                "tp_order_id": 702,
            },
        }

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "_reconcile_pnl_from_binance", AsyncMock()),
            patch.object(server, "trigger_income_sync", AsyncMock()),
            patch.object(server, "_sweep_algo_orders_for", AsyncMock()),
            patch.object(server.binance_live, "place_market_order", AsyncMock(return_value={"executedQty": "10784"})),
            patch.object(server.binance_live, "cancel_algo_order", AsyncMock()),
        ):
            await server._close_position_unsafe("HEMIUSDT_LONG_Recovered", 0.00936, "Manual")
            await server.handle_order_update(
                {
                    "X": "FILLED",
                    "i": 888001,
                    "s": "HEMIUSDT",
                    "ps": "LONG",
                    "S": "SELL",
                    "o": "MARKET",
                    "q": "10784",
                    "ap": "0.00936",
                }
            )

        self.assertNotIn("HEMIUSDT_LONG_Recovered", server.state["open_positions"])
        self.assertIn("HEMIUSDT_Tab7", server.state["open_positions"])

    async def test_bot_close_echo_does_not_hit_lone_sibling_after_target_removed(self):
        """Tab8 dashboard close: fill echo must not auto-close Tab11 on same symbol/side."""
        server.LIVE_MODE = True
        server.state["balances"].update({"Tab8": 10_000.0, "Tab11": 10_000.0})
        server.state["open_positions"] = {
            "BTCUSDT_Tab8": {
                "tab": "Tab8",
                "symbol": "BTCUSDT",
                "side": "Long",
                "position_side": "LONG",
                "entry_price": 65000.0,
                "sl": 64000.0,
                "tp": 67000.0,
                "qty": 0.01,
                "entry_time": "2026-05-26T08:00:00",
                "sl_order_id": 801,
                "tp_order_id": 802,
            },
            "BTCUSDT_Tab11": {
                "tab": "Tab11",
                "symbol": "BTCUSDT",
                "side": "Long",
                "position_side": "LONG",
                "entry_price": 65100.0,
                "sl": 64100.0,
                "tp": 67100.0,
                "qty": 0.01,
                "entry_time": "2026-05-26T09:00:00",
                "sl_order_id": 1101,
                "tp_order_id": 1102,
            },
        }

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "_reconcile_pnl_from_binance", AsyncMock()),
            patch.object(server, "trigger_income_sync", AsyncMock()),
            patch.object(server, "_sweep_algo_orders_for", AsyncMock()),
            patch.object(server, "_live_position_qty", AsyncMock(return_value=0.02)),
            patch.object(server.binance_live, "place_market_order", AsyncMock(return_value={"orderId": 9001, "executedQty": "0.01"})),
            patch.object(server.binance_live, "cancel_algo_order", AsyncMock()),
        ):
            await server._close_position_unsafe("BTCUSDT_Tab8", 65500.0, "Manual")
            await server.handle_order_update(
                {
                    "X": "FILLED",
                    "i": 9001,
                    "s": "BTCUSDT",
                    "ps": "LONG",
                    "S": "SELL",
                    "o": "MARKET",
                    "q": "0.005",
                    "ap": "65500",
                }
            )

        self.assertNotIn("BTCUSDT_Tab8", server.state["open_positions"])
        self.assertIn("BTCUSDT_Tab11", server.state["open_positions"])

    async def test_manual_close_caps_qty_when_sibling_strategy_shares_leg(self):
        server.LIVE_MODE = True
        server.state["balances"].update({"Tab8": 10_000.0, "Tab11": 10_000.0})
        server.state["open_positions"] = {
            "BTCUSDT_Tab8": {
                "tab": "Tab8",
                "symbol": "BTCUSDT",
                "side": "Long",
                "position_side": "LONG",
                "entry_price": 65000.0,
                "sl": 64000.0,
                "tp": 67000.0,
                "qty": 0.02,
                "entry_time": "2026-05-26T08:00:00",
            },
            "BTCUSDT_Tab11": {
                "tab": "Tab11",
                "symbol": "BTCUSDT",
                "side": "Long",
                "position_side": "LONG",
                "entry_price": 65100.0,
                "sl": 64100.0,
                "tp": 67100.0,
                "qty": 0.01,
                "entry_time": "2026-05-26T09:00:00",
            },
        }

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "_reconcile_pnl_from_binance", AsyncMock()),
            patch.object(server, "trigger_income_sync", AsyncMock()),
            patch.object(server, "_sweep_algo_orders_for", AsyncMock()),
            patch.object(server, "_live_position_qty", AsyncMock(return_value=0.02)),
            patch.object(server.binance_live, "place_market_order", AsyncMock(return_value={"orderId": 9002, "executedQty": "0.01"})) as place_market,
            patch.object(server.binance_live, "cancel_algo_order", AsyncMock()),
        ):
            await server._close_position_unsafe("BTCUSDT_Tab8", 65500.0, "Manual")

        place_market.assert_awaited_once()
        self.assertAlmostEqual(float(place_market.await_args.args[3]), 0.01)
        self.assertNotIn("BTCUSDT_Tab8", server.state["open_positions"])
        self.assertIn("BTCUSDT_Tab11", server.state["open_positions"])

    async def test_exchange_sync_close_splits_pnl_by_tab_qty_on_shared_leg(self):
        server.LIVE_MODE = True
        server._sync_close_leg_cache.clear()
        server.state["balances"] = {"Tab11": 10_000.0, "Tab18": 10_000.0}
        server.state["history"] = []
        server.state["open_positions"] = {
            "TACUSDT_Tab11": {
                "tab": "Tab11",
                "symbol": "TACUSDT",
                "side": "Long",
                "position_side": "LONG",
                "entry_price": 0.0237456,
                "qty": 421.0,
                "entry_time": "2026-06-19T09:06:51.289575",
            },
            "TACUSDT_Tab18": {
                "tab": "Tab18",
                "symbol": "TACUSDT",
                "side": "Long",
                "position_side": "LONG",
                "entry_price": 0.02374,
                "qty": 421.0,
                "entry_time": "2026-06-19T09:06:56.577641",
            },
        }
        close_trades = [{
            "symbol": "TACUSDT",
            "side": "SELL",
            "positionSide": "LONG",
            "qty": "842",
            "quoteQty": "18.58976",
            "price": "0.02208",
            "realizedPnl": "-1.40",
            "commission": "0",
            "commissionAsset": "USDT",
        }]
        with (
            patch.object(server.binance_live, "get_account_trades", AsyncMock(return_value=close_trades)),
            patch.object(server, "_sum_trades_commission_parts", return_value=(0.0, 0.0)),
            patch.object(server, "_record_tab_stats_close", lambda *a, **k: None),
            patch.object(server, "_persist_circuit_breaker", lambda: None),
        ):
            await server.record_exchange_sync_close(server.state["open_positions"]["TACUSDT_Tab11"])
            await server.record_exchange_sync_close(server.state["open_positions"]["TACUSDT_Tab18"])

        pnls = [h["pnl_usd"] for h in server.state["history"] if h.get("symbol") == "TACUSDT"]
        self.assertEqual(len(pnls), 2)
        self.assertAlmostEqual(sum(pnls), -1.40, places=2)
        self.assertAlmostEqual(pnls[0], -0.70, places=2)
        self.assertAlmostEqual(pnls[1], -0.70, places=2)
        self.assertAlmostEqual(server.state["balances"]["Tab11"], 9_999.30, places=2)
        self.assertAlmostEqual(server.state["balances"]["Tab18"], 9_999.30, places=2)

    async def test_sync_recovery_only_recreates_missing_leg_for_matching_side(self):
        server.LIVE_MODE = True
        server.state["open_positions"] = {}

        exchange_positions = [
            {
                "symbol": "ETHUSDT",
                "positionAmt": "-0.5",
                "positionSide": "SHORT",
                "entryPrice": "3010.0",
            }
        ]
        account = {"assets": [{"asset": "USDT", "walletBalance": "1000"}]}
        protective_orders = [
            {
                "symbol": "ETHUSDT",
                "algoId": 202,
                "orderType": "TAKE_PROFIT_MARKET",
                "positionSide": "SHORT",
                "triggerPrice": "2890.0",
            }
        ]
        repaired_orders = protective_orders + [
            {
                "symbol": "ETHUSDT",
                "algoId": 301,
                "orderType": "STOP_MARKET",
                "positionSide": "SHORT",
                "triggerPrice": str(3010.0 * 1.025),
            }
        ]

        async def fake_sreq(_client, _method, path, params=None):
            if path == "/fapi/v1/openAlgoOrders" and params and params.get("symbol") == "ETHUSDT":
                return protective_orders
            if path == "/fapi/v1/openAlgoOrders":
                return repaired_orders
            return {}

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "record_sync_issue", AsyncMock()),
            patch.object(server, "get_klines", AsyncMock(return_value=[])),
            patch.object(server, "purge_orphaned_algo_orders", AsyncMock()),
            patch.object(server.binance_live, "get_position_risk", AsyncMock(return_value=exchange_positions)),
            patch.object(server.binance_live, "get_account", AsyncMock(return_value=account)),
            patch.object(server.binance_live, "_sreq", AsyncMock(side_effect=fake_sreq)),
            patch.object(server.binance_live, "place_stop_loss", AsyncMock(return_value={"algoId": 301})) as place_sl,
            patch.object(server.binance_live, "place_take_profit", AsyncMock()) as place_tp,
        ):
            await server.sync_live_positions()

        self.assertEqual(place_sl.await_count, 1)
        place_sl.assert_awaited_once_with(
            server._http_client,
            "ETHUSDT",
            "BUY",
            3010.0 * 1.025,
            0.5,
            position_side="SHORT",
        )
        place_tp.assert_not_awaited()

        self.assertIn("ETHUSDT_SHORT_Recovered", server.state["open_positions"])
        recovered = server.state["open_positions"]["ETHUSDT_SHORT_Recovered"]
        self.assertEqual(recovered["position_side"], "SHORT")
        self.assertEqual(recovered["sl_order_id"], 301)
        self.assertEqual(recovered["tp_order_id"], 202)

    async def test_execute_entry_keeps_unprotected_state_when_rollback_fails(self):
        server.LIVE_MODE = True
        server._circuit_breaker = False
        server.latest_prices["ETHUSDT"] = 100.0
        sig = {"side": "Long", "ep": 100.0, "sl": 96.0, "tp": 108.0}

        async def fake_market_order(_client, _sym, side, *_args, **_kwargs):
            if side == "BUY":
                return {"orderId": 777, "avgPrice": "100.0", "executedQty": "1.0"}
            raise RuntimeError("rollback unavailable")

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "record_error_event", AsyncMock()),
            patch.object(server, "record_sync_issue", AsyncMock()),
            patch.object(server, "_check_entry_quality", AsyncMock(return_value=(True, ""))),
            patch.object(server, "_open_algo_order_count", AsyncMock(return_value=0)),
            patch.object(server.binance_live, "set_margin_type", AsyncMock()),
            patch.object(server.binance_live, "set_leverage", AsyncMock(return_value={"leverage": 5})),
            patch.object(server.binance_live, "place_market_order", AsyncMock(side_effect=fake_market_order)),
            patch.object(server.binance_live, "place_stop_loss", AsyncMock(side_effect=RuntimeError("sl rejected"))),
            patch.object(server.binance_live, "place_take_profit", AsyncMock()) as place_tp,
        ):
            await server._execute_entry_unsafe("ETHUSDT", sig, "Tab1")

        place_tp.assert_not_awaited()
        self.assertIn("ETHUSDT_Tab1", server.state["open_positions"])
        pos = server.state["open_positions"]["ETHUSDT_Tab1"]
        self.assertEqual(pos["protection_status"], "protection_failed")
        self.assertEqual(pos["protection_mode"], "failed")
        self.assertEqual(pos["entry_order_id"], 777)
        self.assertTrue(pos["entry_client_order_id"].startswith("AG_Tab1_L_ENTRY_"))
        self.assertEqual(server.state["position_registry"]["ETHUSDT_Tab1"]["status"], "unprotected")

    async def test_verify_live_position_protection_checks_both_algo_ids(self):
        server.LIVE_MODE = True
        server.state["open_positions"] = {
            "ETHUSDT_Tab1": {
                **self._long_pos(),
                "tab": "Tab1",
                "sl_order_id": 101,
                "tp_order_id": 102,
            }
        }

        with (
            patch.object(server, "_live_position_qty", AsyncMock(return_value=0.5)),
            patch.object(
                server.binance_live,
                "_sreq",
                AsyncMock(return_value=[
                    {"algoId": 101, "positionSide": "LONG"},
                ]),
            ),
        ):
            ok, message = await server._verify_live_position_protection("ETHUSDT_Tab1")

        self.assertFalse(ok)
        self.assertIn("TP", message)

        with (
            patch.object(server, "_live_position_qty", AsyncMock(return_value=0.5)),
            patch.object(
                server.binance_live,
                "_sreq",
                AsyncMock(return_value=[
                    {"algoId": 101, "positionSide": "LONG"},
                    {"algoId": 102, "positionSide": "LONG"},
                ]),
            ),
        ):
            ok, message = await server._verify_live_position_protection("ETHUSDT_Tab1")

        self.assertTrue(ok)
        self.assertIn("verified", message)

    async def test_sync_recovery_uses_position_registry_before_safeguard(self):
        server.LIVE_MODE = True
        server.state["open_positions"] = {}
        server.state["position_registry"] = {
            "ETHUSDT_Tab1": {
                "pos_key": "ETHUSDT_Tab1",
                "tab": "Tab1",
                "symbol": "ETHUSDT",
                "side": "Short",
                "position_side": "SHORT",
                "entry_price": 3010.0,
                "qty": 0.5,
                "status": "open",
            }
        }
        server.latest_prices["BRUSDT"] = 0.162
        exchange_positions = [
            {
                "symbol": "ETHUSDT",
                "positionAmt": "-0.5",
                "positionSide": "SHORT",
                "entryPrice": "3010.0",
            }
        ]
        account = {"assets": [{"asset": "USDT", "walletBalance": "1000"}]}
        repaired_orders = [
            {
                "symbol": "ETHUSDT",
                "algoId": 401,
                "orderType": "STOP_MARKET",
                "positionSide": "SHORT",
                "triggerPrice": str(3010.0 * 1.025),
            },
            {
                "symbol": "ETHUSDT",
                "algoId": 402,
                "orderType": "TAKE_PROFIT_MARKET",
                "positionSide": "SHORT",
                "triggerPrice": str(3010.0 * 0.95),
            },
        ]

        async def fake_sreq(_client, _method, path, params=None):
            if path == "/fapi/v1/openAlgoOrders" and params and params.get("symbol") == "ETHUSDT":
                return []
            if path == "/fapi/v1/openAlgoOrders":
                return repaired_orders
            return {}

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "record_sync_issue", AsyncMock()),
            patch.object(server, "get_klines", AsyncMock(return_value=[])),
            patch.object(server, "purge_orphaned_algo_orders", AsyncMock()),
            patch.object(server.binance_live, "get_position_risk", AsyncMock(return_value=exchange_positions)),
            patch.object(server.binance_live, "get_account", AsyncMock(return_value=account)),
            patch.object(server.binance_live, "_sreq", AsyncMock(side_effect=fake_sreq)),
            patch.object(server.binance_live, "place_stop_loss", AsyncMock(return_value={"algoId": 401})),
            patch.object(server.binance_live, "place_take_profit", AsyncMock(return_value={"algoId": 402})),
        ):
            await server.sync_live_positions()

        self.assertIn("ETHUSDT_Tab1", server.state["open_positions"])
        recovered = server.state["open_positions"]["ETHUSDT_Tab1"]
        self.assertEqual(recovered["tab"], "Tab1")
        self.assertEqual(recovered["recovery_source"], "position registry ETHUSDT_Tab1")
        self.assertEqual(server.state["position_registry"]["ETHUSDT_Tab1"]["status"], "open")

    async def test_sync_recovers_exchange_qty_excess_as_safeguard_leg(self):
        server.LIVE_MODE = True
        server.state["open_positions"] = {
            "HEMIUSDT_Tab7": {
                "tab": "Tab7",
                "symbol": "HEMIUSDT",
                "side": "Long",
                "position_side": "LONG",
                "entry_price": 0.0093,
                "sl": 0.0088,
                "tp": 0.0101,
                "qty": 10784.0,
                "entry_time": "2026-04-26T13:06:00",
                "sl_order_id": 701,
                "tp_order_id": 702,
            },
            "HEMIUSDT_Tab3": {
                "tab": "Tab3",
                "symbol": "HEMIUSDT",
                "side": "Long",
                "position_side": "LONG",
                "entry_price": 0.0094,
                "sl": 0.0086,
                "tp": 0.0124,
                "qty": 10616.0,
                "entry_time": "2026-04-26T16:01:00",
                "sl_order_id": 703,
                "tp_order_id": 704,
            },
        }
        exchange_positions = [
            {
                "symbol": "HEMIUSDT",
                "positionAmt": "32184",
                "positionSide": "LONG",
                "entryPrice": "0.0093194780015",
            }
        ]
        account = {"assets": [{"asset": "USDT", "walletBalance": "1000"}]}
        open_algo_orders = [
            {"symbol": "HEMIUSDT", "algoId": 701, "orderType": "STOP_MARKET", "positionSide": "LONG", "quantity": "10784", "triggerPrice": "0.0088"},
            {"symbol": "HEMIUSDT", "algoId": 702, "orderType": "TAKE_PROFIT_MARKET", "positionSide": "LONG", "quantity": "10784", "triggerPrice": "0.0101"},
            {"symbol": "HEMIUSDT", "algoId": 703, "orderType": "STOP_MARKET", "positionSide": "LONG", "quantity": "10616", "triggerPrice": "0.0086"},
            {"symbol": "HEMIUSDT", "algoId": 704, "orderType": "TAKE_PROFIT_MARKET", "positionSide": "LONG", "quantity": "10616", "triggerPrice": "0.0124"},
            {"symbol": "HEMIUSDT", "algoId": 901, "orderType": "STOP_MARKET", "positionSide": "LONG", "quantity": "10784", "triggerPrice": "0.0091"},
            {"symbol": "HEMIUSDT", "algoId": 902, "orderType": "TAKE_PROFIT_MARKET", "positionSide": "LONG", "quantity": "10784", "triggerPrice": "0.0098"},
        ]

        async def fake_sreq(_client, _method, path, params=None):
            if path == "/fapi/v1/openAlgoOrders":
                return open_algo_orders
            return {}

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "record_sync_issue", AsyncMock()),
            patch.object(server, "get_klines", AsyncMock(return_value=[])),
            patch.object(server, "purge_orphaned_algo_orders", AsyncMock()),
            patch.object(server.binance_live, "get_position_risk", AsyncMock(return_value=exchange_positions)),
            patch.object(server.binance_live, "get_account", AsyncMock(return_value=account)),
            patch.object(server.binance_live, "_sreq", AsyncMock(side_effect=fake_sreq)),
            patch.object(server.binance_live, "place_stop_loss", AsyncMock(return_value={"algoId": 901})) as place_sl,
            patch.object(server.binance_live, "place_take_profit", AsyncMock(return_value={"algoId": 902})) as place_tp,
        ):
            await server.sync_live_positions()

        place_sl.assert_awaited_once()
        place_tp.assert_awaited_once()
        self.assertIn("HEMIUSDT_LONG_Recovered", server.state["open_positions"])
        recovered = server.state["open_positions"]["HEMIUSDT_LONG_Recovered"]
        self.assertEqual(recovered["tab"], "SafeGuard")
        self.assertEqual(recovered["qty"], 10784.0)
        self.assertEqual(recovered["sl_order_id"], 901)
        self.assertEqual(recovered["tp_order_id"], 902)
        self.assertIn("exchange qty excess", recovered["recovery_source"])

    async def test_sync_defers_qty_mismatch_when_same_side_entries_are_recent(self):
        server.LIVE_MODE = True
        server.BINANCE_TESTNET = True
        server.state["sltp_mode"] = "local"
        server.state["balances"].update({"Tab2": 10_000.0, "Tab9": 10_000.0, "Tab10": 10_000.0})
        recent_time = datetime.now().isoformat()
        old_time = (datetime.now() - timedelta(hours=2)).isoformat()
        server.state["open_positions"] = {
            "XRPUSDT_Tab2": {
                "tab": "Tab2",
                "symbol": "XRPUSDT",
                "side": "Short",
                "position_side": "SHORT",
                "entry_price": 1.42,
                "sl": 1.45,
                "tp": 1.38,
                "qty": 70.4,
                "entry_time": old_time,
                "sl_order_id": 201,
                "tp_order_id": 202,
            },
            "XRPUSDT_Tab9": {
                "tab": "Tab9",
                "symbol": "XRPUSDT",
                "side": "Short",
                "position_side": "SHORT",
                "entry_price": 1.4185,
                "sl": 1.4351,
                "tp": 1.3977,
                "qty": 70.5,
                "entry_time": recent_time,
                "protection_mode": "local",
            },
            "XRPUSDT_Tab10": {
                "tab": "Tab10",
                "symbol": "XRPUSDT",
                "side": "Short",
                "position_side": "SHORT",
                "entry_price": 1.4185,
                "sl": 1.4327,
                "tp": 1.3971,
                "qty": 70.5,
                "entry_time": recent_time,
                "protection_mode": "local",
            },
        }
        exchange_positions = [
            {
                "symbol": "XRPUSDT",
                "positionAmt": "-70.4",
                "positionSide": "SHORT",
                "entryPrice": "1.4176",
                "markPrice": "1.4170",
            }
        ]
        account = {"assets": [{"asset": "USDT", "walletBalance": "1000"}]}

        async def fake_sreq(_client, _method, path, params=None):
            if path == "/fapi/v1/openAlgoOrders":
                return [
                    {"symbol": "XRPUSDT", "algoId": 201, "orderType": "STOP_MARKET", "positionSide": "SHORT", "quantity": "70.4", "triggerPrice": "1.45"},
                    {"symbol": "XRPUSDT", "algoId": 202, "orderType": "TAKE_PROFIT_MARKET", "positionSide": "SHORT", "quantity": "70.4", "triggerPrice": "1.38"},
                ]
            return {}

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "record_sync_issue", AsyncMock()),
            patch.object(server, "get_klines", AsyncMock(return_value=[])),
            patch.object(server, "purge_orphaned_algo_orders", AsyncMock()),
            patch.object(server.binance_live, "get_position_risk", AsyncMock(return_value=exchange_positions)),
            patch.object(server.binance_live, "get_account", AsyncMock(return_value=account)),
            patch.object(server.binance_live, "_sreq", AsyncMock(side_effect=fake_sreq)),
            patch.object(server.binance_live, "place_stop_loss", AsyncMock(return_value={"algoId": 301})) as place_sl,
            patch.object(server.binance_live, "place_take_profit", AsyncMock(return_value={"algoId": 302})) as place_tp,
        ):
            await server.sync_live_positions()

        self.assertIn("XRPUSDT_Tab2", server.state["open_positions"])
        self.assertIn("XRPUSDT_Tab9", server.state["open_positions"])
        self.assertIn("XRPUSDT_Tab10", server.state["open_positions"])
        self.assertNotIn("XRPUSDT_SHORT_Recovered", server.state["open_positions"])
        self.assertEqual(server.state["open_positions"]["XRPUSDT_Tab9"].get("protection_mode"), "local")
        self.assertIsNone(server.state["open_positions"]["XRPUSDT_Tab9"].get("sl_order_id"))
        self.assertEqual(server.state["open_positions"]["XRPUSDT_Tab10"].get("protection_mode"), "local")
        self.assertIsNone(server.state["open_positions"]["XRPUSDT_Tab10"].get("sl_order_id"))
        place_sl.assert_not_awaited()
        place_tp.assert_not_awaited()

    async def test_sync_does_not_repair_local_protection_with_algo_orders(self):
        server.LIVE_MODE = True
        server.BINANCE_TESTNET = True
        server.state["sltp_mode"] = "local"
        server.state["open_positions"] = {
            "BASUSDT_Tab1": {
                "tab": "Tab1",
                "symbol": "BASUSDT",
                "side": "Long",
                "position_side": "LONG",
                "entry_price": 0.016,
                "sl": 0.0155,
                "tp": 0.017,
                "qty": 5997.4,
                "entry_time": "2026-04-27T09:39:30",
                "sl_order_id": None,
                "tp_order_id": None,
                "protection_mode": "local",
                "protection_reason": "testnet_local_policy",
                "protection_status": "local",
            }
        }
        exchange_positions = [
            {
                "symbol": "BASUSDT",
                "positionAmt": "5997.4",
                "positionSide": "LONG",
                "entryPrice": "0.016",
                "markPrice": "0.0162",
            }
        ]
        account = {"assets": [{"asset": "USDT", "walletBalance": "1000"}]}

        async def fake_sreq(_client, _method, path, params=None):
            if path == "/fapi/v1/openAlgoOrders":
                return []
            return {}

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "record_sync_issue", AsyncMock()),
            patch.object(server, "get_klines", AsyncMock(return_value=[])),
            patch.object(server, "purge_orphaned_algo_orders", AsyncMock()),
            patch.object(server.binance_live, "get_position_risk", AsyncMock(return_value=exchange_positions)),
            patch.object(server.binance_live, "get_account", AsyncMock(return_value=account)),
            patch.object(server.binance_live, "_sreq", AsyncMock(side_effect=fake_sreq)),
            patch.object(server.binance_live, "place_stop_loss", AsyncMock()) as place_sl,
            patch.object(server.binance_live, "place_take_profit", AsyncMock()) as place_tp,
        ):
            await server.sync_live_positions()

        place_sl.assert_not_awaited()
        place_tp.assert_not_awaited()
        pos = server.state["open_positions"]["BASUSDT_Tab1"]
        self.assertEqual(pos["protection_mode"], "local")
        self.assertEqual(pos["protection_reason"], "testnet_local_policy")

    async def test_sync_repairs_missing_exchange_protection_when_local_sltp_off(self):
        server.LIVE_MODE = True
        server.BINANCE_TESTNET = True
        server.state["sltp_mode"] = "binance"
        server.state["open_positions"] = {
            "BASUSDT_Tab11": {
                "tab": "Tab11",
                "symbol": "BASUSDT",
                "side": "Long",
                "position_side": "LONG",
                "entry_price": 0.016,
                "sl": 0.0155,
                "tp": 0.017,
                "qty": 5997.4,
                "entry_time": "2026-04-27T09:39:30",
                "sl_order_id": None,
                "tp_order_id": None,
                "protection_mode": "local",
                "protection_reason": "testnet_local_policy",
                "protection_status": "local",
            }
        }
        exchange_positions = [
            {
                "symbol": "BASUSDT",
                "positionAmt": "5997.4",
                "positionSide": "LONG",
                "entryPrice": "0.016",
                "markPrice": "0.0162",
            }
        ]
        account = {"assets": [{"asset": "USDT", "walletBalance": "1000"}]}

        async def fake_sreq(_client, _method, path, params=None):
            if path == "/fapi/v1/openAlgoOrders":
                return []
            if path == "/fapi/v1/premiumIndex":
                return {"markPrice": "0.0162"}
            return {}

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "record_sync_issue", AsyncMock()),
            patch.object(server, "get_klines", AsyncMock(return_value=[])),
            patch.object(server, "purge_orphaned_algo_orders", AsyncMock()),
            patch.object(server.binance_live, "get_position_risk", AsyncMock(return_value=exchange_positions)),
            patch.object(server.binance_live, "get_account", AsyncMock(return_value=account)),
            patch.object(server.binance_live, "_sreq", AsyncMock(side_effect=fake_sreq)),
            patch.object(server.binance_live, "place_stop_loss", AsyncMock(return_value={"algoId": 901})) as place_sl,
            patch.object(server.binance_live, "place_take_profit", AsyncMock(return_value={"algoId": 902})) as place_tp,
        ):
            await server.sync_live_positions()

        place_sl.assert_awaited_once()
        place_tp.assert_awaited_once()
        pos = server.state["open_positions"]["BASUSDT_Tab11"]
        self.assertEqual(pos["protection_status"], "exchange")
        self.assertNotIn("protection_mode", pos)
        self.assertEqual(pos["sl_order_id"], 901)
        self.assertEqual(pos["tp_order_id"], 902)

    async def test_testnet_sync_closes_tab11_when_missing_tp_already_crossed(self):
        server.LIVE_MODE = True
        server.BINANCE_TESTNET = True
        server.state["open_positions"] = {
            "BRUSDT_Tab11": {
                "tab": "Tab11",
                "symbol": "BRUSDT",
                "side": "Long",
                "position_side": "LONG",
                "entry_price": 0.10941,
                "sl": 0.1017514,
                "tp": 0.1189832,
                "qty": 914.0,
                "entry_time": "2026-04-30T20:01:30",
                "sl_order_id": 1001,
                "tp_order_id": 1002,
                "protection_status": "exchange",
            }
        }
        server.latest_prices["BRUSDT"] = 0.162
        exchange_positions = [
            {
                "symbol": "BRUSDT",
                "positionAmt": "914",
                "positionSide": "LONG",
                "entryPrice": "0.10941",
                "markPrice": "0.162",
            }
        ]
        account = {"assets": [{"asset": "USDT", "walletBalance": "1000"}]}

        async def fake_sreq(_client, _method, path, params=None):
            if path == "/fapi/v1/openAlgoOrders":
                return [
                    {
                        "symbol": "BRUSDT",
                        "positionSide": "LONG",
                        "algoId": 1001,
                        "clientAlgoId": "AG_Tab11_L_SL_1",
                        "orderType": "STOP_MARKET",
                        "quantity": "914",
                        "triggerPrice": "0.1017514",
                        "algoStatus": "NEW",
                    }
                ]
            if path == "/fapi/v1/algoOrder":
                return {"algoStatus": "NEW"}
            if path == "/fapi/v1/premiumIndex":
                return {"markPrice": "0.162"}
            return {}

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "record_sync_issue", AsyncMock()),
            patch.object(server, "get_klines", AsyncMock(return_value=[])),
            patch.object(server, "purge_orphaned_algo_orders", AsyncMock()),
            patch.object(server.binance_live, "get_position_risk", AsyncMock(return_value=exchange_positions)),
            patch.object(server.binance_live, "get_account", AsyncMock(return_value=account)),
            patch.object(server.binance_live, "_sreq", AsyncMock(side_effect=fake_sreq)),
            patch.object(server, "_close_position_unsafe", AsyncMock()) as close_pos,
            patch.object(server.binance_live, "place_take_profit", AsyncMock()) as place_tp,
        ):
            await server.sync_live_positions()

        place_tp.assert_not_awaited()
        close_pos.assert_awaited_once_with("BRUSDT_Tab11", 0.162, "TP")

    async def test_sync_closes_hybrid_when_missing_sl_crossed_mark(self):
        server.LIVE_MODE = True
        server.BINANCE_TESTNET = False
        server.state["sltp_mode"] = "hybrid"
        server.state["open_positions"] = {
            "HUSDT_Tab7": {
                "tab": "Tab7",
                "symbol": "HUSDT",
                "side": "Long",
                "position_side": "LONG",
                "entry_price": 0.72,
                "sl": 0.7009195,
                "tp": 0.75,
                "qty": 14.0,
                "entry_time": "2026-06-07T08:19:37",
                "sl_order_id": 4000001509963964,
                "tp_order_id": None,
                "protection_mode": "hybrid",
                "sl_source": "exchange",
                "tp_source": "local",
                "protection_status": "hybrid",
            }
        }
        exchange_positions = [
            {
                "symbol": "HUSDT",
                "positionAmt": "14",
                "positionSide": "LONG",
                "entryPrice": "0.72",
                "markPrice": "0.69",
            }
        ]
        account = {"assets": [{"asset": "USDT", "walletBalance": "1000"}]}
        server.latest_prices["HUSDT"] = 0.69

        async def fake_sreq(_client, _method, path, params=None):
            if path == "/fapi/v1/openAlgoOrders":
                return []
            return {}

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "record_sync_issue", AsyncMock()),
            patch.object(server, "get_klines", AsyncMock(return_value=[])),
            patch.object(server, "purge_orphaned_algo_orders", AsyncMock()),
            patch.object(server.binance_live, "get_position_risk", AsyncMock(return_value=exchange_positions)),
            patch.object(server.binance_live, "get_account", AsyncMock(return_value=account)),
            patch.object(server.binance_live, "_sreq", AsyncMock(side_effect=fake_sreq)),
            patch.object(server, "_close_position_unsafe", AsyncMock()) as close_pos,
            patch.object(server.binance_live, "place_stop_loss", AsyncMock()) as place_sl,
        ):
            await server.sync_live_positions()

        place_sl.assert_not_awaited()
        close_pos.assert_awaited_once_with("HUSDT_Tab7", 0.69, "SL")

    async def test_sync_closes_on_sl_immediate_trigger_reject(self):
        server.LIVE_MODE = True
        server.BINANCE_TESTNET = False
        server.state["sltp_mode"] = "binance"
        server.state["open_positions"] = {
            "HUSDT_Tab7": {
                "tab": "Tab7",
                "symbol": "HUSDT",
                "side": "Long",
                "position_side": "LONG",
                "entry_price": 0.72,
                "sl": 0.7009195,
                "tp": 0.75,
                "qty": 14.0,
                "entry_time": "2026-06-07T08:19:37",
                "sl_order_id": None,
                "tp_order_id": 4000001509963966,
                "protection_status": "exchange",
            }
        }
        exchange_positions = [
            {
                "symbol": "HUSDT",
                "positionAmt": "14",
                "positionSide": "LONG",
                "entryPrice": "0.72",
                "markPrice": "0.705",
            }
        ]
        account = {"assets": [{"asset": "USDT", "walletBalance": "1000"}]}

        class _RejectResponse:
            text = '{"code":-2021,"msg":"Order would immediately trigger."}'

        class _RejectError(Exception):
            response = _RejectResponse()

        async def fake_sreq(_client, _method, path, params=None):
            if path == "/fapi/v1/openAlgoOrders":
                return [
                    {
                        "symbol": "HUSDT",
                        "positionSide": "LONG",
                        "algoId": 4000001509963966,
                        "orderType": "TAKE_PROFIT_MARKET",
                        "quantity": "14",
                        "triggerPrice": "0.75",
                        "algoStatus": "NEW",
                    }
                ]
            return {}

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "record_sync_issue", AsyncMock()),
            patch.object(server, "get_klines", AsyncMock(return_value=[])),
            patch.object(server, "purge_orphaned_algo_orders", AsyncMock()),
            patch.object(server.binance_live, "get_position_risk", AsyncMock(return_value=exchange_positions)),
            patch.object(server.binance_live, "get_account", AsyncMock(return_value=account)),
            patch.object(server.binance_live, "_sreq", AsyncMock(side_effect=fake_sreq)),
            patch.object(server, "_fetch_sltp_trigger_price", AsyncMock(return_value=0.705)),
            patch.object(server, "_close_position_unsafe", AsyncMock()) as close_pos,
            patch.object(server.binance_live, "place_stop_loss", AsyncMock(side_effect=_RejectError())) as place_sl,
        ):
            await server.sync_live_positions()

        place_sl.assert_awaited_once()
        close_pos.assert_awaited_once_with("HUSDT_Tab7", 0.705, "SL")

    def test_exchange_sl_crossed_mark_uses_nudge_buffer(self):
        self.assertTrue(server._exchange_sl_crossed_mark(True, 0.7009195, 0.69))
        self.assertTrue(server._exchange_sl_crossed_mark(True, 0.7009195, 0.701))
        self.assertFalse(server._exchange_sl_crossed_mark(True, 0.690, 0.705))

    def test_is_immediate_trigger_error_detects_binance_2021(self):
        class _Resp:
            text = '{"code":-2021,"msg":"Order would immediately trigger."}'

        class _Err(Exception):
            response = _Resp()

        self.assertTrue(server._is_immediate_trigger_error(_Err()))

    async def test_sync_keeps_local_protection_and_cancels_exchange_legs(self):
        server.LIVE_MODE = True
        server.BINANCE_TESTNET = True
        server.state["sltp_mode"] = "local"
        server.state["open_positions"] = {
            "BASUSDT_Tab12": {
                "tab": "Tab12",
                "symbol": "BASUSDT",
                "side": "Long",
                "position_side": "LONG",
                "entry_price": 0.016,
                "sl": 0.0155,
                "tp": 0.017,
                "qty": 5997.4,
                "entry_time": "2026-04-27T09:39:30",
                "sl_order_id": 801,
                "tp_order_id": 802,
                "protection_mode": "local",
                "protection_reason": "testnet_local_policy",
                "protection_status": "local",
            }
        }
        exchange_positions = [
            {
                "symbol": "BASUSDT",
                "positionAmt": "5997.4",
                "positionSide": "LONG",
                "entryPrice": "0.016",
                "markPrice": "0.0162",
            }
        ]
        account = {"assets": [{"asset": "USDT", "walletBalance": "1000"}]}

        async def fake_sreq(_client, _method, path, params=None):
            if path == "/fapi/v1/openAlgoOrders":
                return [
                    {
                        "symbol": "BASUSDT",
                        "algoId": 801,
                        "orderType": "STOP_MARKET",
                        "positionSide": "LONG",
                        "quantity": "5997.4",
                        "triggerPrice": "0.0155",
                        "clientAlgoId": "AG_Tab12_L_SL_260427093930241",
                    },
                    {
                        "symbol": "BASUSDT",
                        "algoId": 802,
                        "orderType": "TAKE_PROFIT_MARKET",
                        "positionSide": "LONG",
                        "quantity": "5997.4",
                        "triggerPrice": "0.017",
                        "clientAlgoId": "AG_Tab12_L_TP_260427093930241",
                    },
                ]
            if path == "/fapi/v1/premiumIndex":
                return {"markPrice": "0.0162"}
            return {}

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "record_sync_issue", AsyncMock()),
            patch.object(server, "get_klines", AsyncMock(return_value=[])),
            patch.object(server, "purge_orphaned_algo_orders", AsyncMock()),
            patch.object(server.binance_live, "get_position_risk", AsyncMock(return_value=exchange_positions)),
            patch.object(server.binance_live, "get_account", AsyncMock(return_value=account)),
            patch.object(server.binance_live, "_sreq", AsyncMock(side_effect=fake_sreq)),
            patch.object(server.binance_live, "cancel_algo_order", AsyncMock()) as cancel_algo,
            patch.object(server.binance_live, "place_stop_loss", AsyncMock()) as place_sl,
            patch.object(server.binance_live, "place_take_profit", AsyncMock()) as place_tp,
        ):
            await server.sync_live_positions()

        place_sl.assert_not_awaited()
        place_tp.assert_not_awaited()
        pos = server.state["open_positions"]["BASUSDT_Tab12"]
        self.assertEqual(pos["protection_status"], "local")
        self.assertEqual(pos["protection_mode"], "local")
        self.assertEqual(pos["protection_reason"], server._MAINNET_LOCAL_SLTP_REASON)
        self.assertEqual(cancel_algo.await_count, 2)
        cancel_algo.assert_any_await(server._http_client, algo_id=801)
        cancel_algo.assert_any_await(server._http_client, algo_id=802)
        self.assertNotIn("BASUSDT_Tab12_2", server.state["open_positions"])

    async def test_verified_active_algo_orders_filters_terminal_detail_rows(self):
        raw = [
            {"symbol": "ETHUSDT", "algoId": 1, "algoStatus": "NEW"},
            {"symbol": "ETHUSDT", "algoId": 2, "algoStatus": "NEW"},
            {"symbol": "ETHUSDT", "algoId": 3, "algoStatus": "CANCELED"},
        ]

        async def fake_sreq(_client, _method, _path, params):
            if params.get("algoId") == 1:
                return {"symbol": "ETHUSDT", "algoId": 1, "algoStatus": "NEW"}
            return {"symbol": "ETHUSDT", "algoId": 2, "algoStatus": "FINISHED"}

        with patch.object(server.binance_live, "_sreq", AsyncMock(side_effect=fake_sreq)):
            active = await server._verified_active_algo_orders(object(), raw)

        self.assertEqual([o["algoId"] for o in active], [1])

    async def test_entry_preflight_skip_prevents_market_order(self):
        server.LIVE_MODE = True
        server._circuit_breaker = False
        sig = {"side": "Long", "ep": 100.0, "sl": 101.0, "tp": 102.0}

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "record_error_event", AsyncMock()) as record_error,
            patch.object(server, "_check_entry_quality", AsyncMock(return_value=(True, ""))),
            patch.object(server, "_open_algo_order_count", AsyncMock(return_value=0)),
            patch.object(server, "_simulate_entry_protection", AsyncMock(return_value=(False, "bad protection", 0, 0, 100))),
            patch.object(server.binance_live, "set_margin_type", AsyncMock()),
            patch.object(server.binance_live, "set_leverage", AsyncMock(return_value={"leverage": 5})),
            patch.object(server.binance_live, "place_market_order", AsyncMock()) as place_market,
        ):
            await server._execute_entry_unsafe("ETHUSDT", sig, "Tab1")

        place_market.assert_not_awaited()
        record_error.assert_awaited_once()
        self.assertNotIn("ETHUSDT_Tab1", server.state["open_positions"])

    async def test_local_sltp_entry_uses_local_protection_without_algo_orders(self):
        server.LIVE_MODE = True
        server.BINANCE_TESTNET = True
        server.state["sltp_mode"] = "local"
        server._circuit_breaker = False
        server.latest_prices["ETHUSDT"] = 100.0
        sig = {"side": "Long", "ep": 100.0, "sl": 96.0, "tp": 108.0}

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "record_error_event", AsyncMock()),
            patch.object(server, "record_sync_issue", AsyncMock()),
            patch.object(server, "_check_entry_quality", AsyncMock(return_value=(True, ""))),
            patch.object(server, "_live_position_qty", AsyncMock(return_value=1.0)),
            patch.object(server, "_verify_live_position_protection", AsyncMock(return_value=(True, ""))),
            patch.object(server, "_alert_position_protection_risk", AsyncMock()) as alert_local,
            patch.object(server.binance_live, "_sreq", AsyncMock(return_value={"markPrice": "100.0"})),
            patch.object(server.binance_live, "set_margin_type", AsyncMock()),
            patch.object(server.binance_live, "set_leverage", AsyncMock(return_value={"leverage": 5})),
            patch.object(
                server.binance_live,
                "place_market_order",
                AsyncMock(return_value={"orderId": 777, "avgPrice": "100.0", "executedQty": "1.0"}),
            ),
            patch.object(server.binance_live, "place_stop_loss", AsyncMock()) as place_sl,
            patch.object(server.binance_live, "place_take_profit", AsyncMock()) as place_tp,
        ):
            await server._execute_entry_unsafe("ETHUSDT", sig, "Tab9")

        place_sl.assert_not_awaited()
        place_tp.assert_not_awaited()
        alert_local.assert_awaited_once()
        pos = server.state["open_positions"]["ETHUSDT_Tab9"]
        self.assertEqual(pos["protection_status"], "local")
        self.assertEqual(pos["protection_mode"], "local")
        self.assertEqual(pos["protection_reason"], "mainnet_local_sl_tp")
        self.assertIsNone(pos["sl_order_id"])
        self.assertIsNone(pos["tp_order_id"])

    async def test_exchange_sltp_entry_uses_algo_orders(self):
        server.LIVE_MODE = True
        server.BINANCE_TESTNET = True
        server.state["sltp_mode"] = "binance"
        server._circuit_breaker = False
        server.latest_prices["ETHUSDT"] = 100.0
        sig = {"side": "Long", "ep": 100.0, "sl": 96.0, "tp": 108.0}

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "record_error_event", AsyncMock()),
            patch.object(server, "record_sync_issue", AsyncMock()),
            patch.object(server, "_check_entry_quality", AsyncMock(return_value=(True, ""))),
            patch.object(server, "_open_algo_order_count", AsyncMock(return_value=0)),
            patch.object(server, "_live_position_qty", AsyncMock(return_value=1.0)),
            patch.object(server, "_verify_live_position_protection", AsyncMock(return_value=(True, ""))),
            patch.object(server, "_alert_position_protection_risk", AsyncMock()),
            patch.object(server.binance_live, "_sreq", AsyncMock(return_value={"markPrice": "100.0"})),
            patch.object(server.binance_live, "set_margin_type", AsyncMock()),
            patch.object(server.binance_live, "set_leverage", AsyncMock(return_value={"leverage": 5})),
            patch.object(
                server.binance_live,
                "place_market_order",
                AsyncMock(return_value={"orderId": 777, "avgPrice": "100.0", "executedQty": "1.0"}),
            ),
            patch.object(server.binance_live, "place_stop_loss", AsyncMock(return_value={"algoId": 301})) as place_sl,
            patch.object(server.binance_live, "place_take_profit", AsyncMock(return_value={"algoId": 302})) as place_tp,
        ):
            await server._execute_entry_unsafe("ETHUSDT", sig, "Tab10")

        place_sl.assert_awaited_once()
        place_tp.assert_awaited_once()
        pos = server.state["open_positions"]["ETHUSDT_Tab10"]
        self.assertEqual(pos["protection_status"], "exchange")
        self.assertNotIn("protection_mode", pos)
        self.assertIsNotNone(pos["sl_order_id"])
        self.assertIsNotNone(pos["tp_order_id"])

    async def test_hybrid_sltp_entry_places_sl_only(self):
        server.LIVE_MODE = True
        server.BINANCE_TESTNET = True
        server.state["sltp_mode"] = "hybrid"
        server._circuit_breaker = False
        server.latest_prices["ETHUSDT"] = 100.0
        sig = {"side": "Long", "ep": 100.0, "sl": 96.0, "tp": 108.0}

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "record_error_event", AsyncMock()),
            patch.object(server, "record_sync_issue", AsyncMock()),
            patch.object(server, "_check_entry_quality", AsyncMock(return_value=(True, ""))),
            patch.object(server, "_open_algo_order_count", AsyncMock(return_value=0)),
            patch.object(server, "_live_position_qty", AsyncMock(return_value=1.0)),
            patch.object(server, "_verify_live_position_protection", AsyncMock(return_value=(True, ""))),
            patch.object(server, "_alert_position_protection_risk", AsyncMock()),
            patch.object(server.binance_live, "_sreq", AsyncMock(return_value={"markPrice": "100.0"})),
            patch.object(server.binance_live, "set_margin_type", AsyncMock()),
            patch.object(server.binance_live, "set_leverage", AsyncMock(return_value={"leverage": 5})),
            patch.object(
                server.binance_live,
                "place_market_order",
                AsyncMock(return_value={"orderId": 778, "avgPrice": "100.0", "executedQty": "1.0"}),
            ),
            patch.object(server.binance_live, "place_stop_loss", AsyncMock(return_value={"algoId": 401})) as place_sl,
            patch.object(server.binance_live, "place_take_profit", AsyncMock()) as place_tp,
        ):
            await server._execute_entry_unsafe("ETHUSDT", sig, "Tab12")

        place_sl.assert_awaited_once()
        place_tp.assert_not_awaited()
        pos = server.state["open_positions"]["ETHUSDT_Tab12"]
        self.assertEqual(pos["protection_mode"], "hybrid")
        self.assertEqual(pos["sl_source"], "exchange")
        self.assertEqual(pos["tp_source"], "local")
        self.assertIsNotNone(pos["sl_order_id"])
        self.assertIsNone(pos["tp_order_id"])

    async def test_binance_fallback_uses_local_when_algo_cap_low(self):
        server.LIVE_MODE = True
        server.BINANCE_TESTNET = False
        server.state["sltp_mode"] = "binance_fallback"
        server._circuit_breaker = False
        server.latest_prices["ETHUSDT"] = 100.0
        sig = {"side": "Long", "ep": 100.0, "sl": 96.0, "tp": 108.0}

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "record_error_event", AsyncMock()),
            patch.object(server, "record_sync_issue", AsyncMock()),
            patch.object(server, "_check_entry_quality", AsyncMock(return_value=(True, ""))),
            patch.object(server, "_open_algo_order_count", AsyncMock(return_value=199)),
            patch.object(server, "_live_position_qty", AsyncMock(return_value=1.0)),
            patch.object(server, "_verify_live_position_protection", AsyncMock(return_value=(True, ""))),
            patch.object(server, "_alert_position_protection_risk", AsyncMock()),
            patch.object(server.binance_live, "_sreq", AsyncMock(return_value={"markPrice": "100.0"})),
            patch.object(server.binance_live, "set_margin_type", AsyncMock()),
            patch.object(server.binance_live, "set_leverage", AsyncMock(return_value={"leverage": 5})),
            patch.object(
                server.binance_live,
                "place_market_order",
                AsyncMock(return_value={"orderId": 779, "avgPrice": "100.0", "executedQty": "1.0"}),
            ),
            patch.object(server.binance_live, "place_stop_loss", AsyncMock()) as place_sl,
            patch.object(server.binance_live, "place_take_profit", AsyncMock()) as place_tp,
        ):
            await server._execute_entry_unsafe("ETHUSDT", sig, "Tab13")

        place_sl.assert_not_awaited()
        place_tp.assert_not_awaited()
        pos = server.state["open_positions"]["ETHUSDT_Tab13"]
        self.assertEqual(pos["protection_mode"], "local")
        self.assertEqual(pos["protection_reason"], server._FALLBACK_LOCAL_REASON)

    async def test_paper_entry_and_close_apply_fee_and_slippage(self):
        server.LIVE_MODE = False
        server._circuit_breaker = False
        server.latest_prices["ETHUSDT"] = 100.0
        server.latest_marks["ETHUSDT"] = 100.0
        sig = {"side": "Long", "ep": 100.0, "sl": 95.0, "tp": 110.0}

        async def fake_price_feed(path, params=None):
            if path == "/fapi/v1/premiumIndex":
                return {"markPrice": "100.0"}
            if path == "/fapi/v1/ticker/bookTicker":
                return {"bidPrice": "99.95", "askPrice": "100.05"}
            return {}

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "_check_entry_quality", AsyncMock(return_value=(True, ""))),
            patch.object(server, "_price_feed_get", AsyncMock(side_effect=fake_price_feed)),
            patch.object(server, "SLIPPAGE_PCT", 0.0003),
            patch.object(server, "ENTRY_FEE_PCT", 0.0005),
            patch.object(server, "EXIT_FEE_MAKER_PCT", 0.0002),
            patch.object(server, "EXIT_FEE_TAKER_PCT", 0.0005),
        ):
            await server._execute_entry_unsafe("ETHUSDT", sig, "TabA")

            pos = server.state["open_positions"]["ETHUSDT_TabA"]
            expected_entry = 100.05 * 1.0003
            expected_qty = server.binance_live.round_qty(
                "ETHUSDT", server.NOTIONAL_SIZE / expected_entry
            )
            self.assertAlmostEqual(pos["entry_price"], expected_entry)
            self.assertAlmostEqual(pos["qty"], expected_qty)
            self.assertAlmostEqual(pos["sl"], expected_entry - 5.0)
            self.assertAlmostEqual(pos["tp"], expected_entry + 10.0)
            self.assertAlmostEqual(pos["entry_slippage_usd"], (expected_entry - 100.0) * expected_qty)

            await server._close_position_unsafe("ETHUSDT_TabA", pos["tp"], "TP")

        hist = server.state["history"][-1]
        tp_trigger = expected_entry + 10.0
        exit_price = tp_trigger * (1 - 0.0003)
        expected_fee = (expected_entry * expected_qty) * 0.0005 + (exit_price * expected_qty) * 0.0005
        expected_gross = (exit_price - expected_entry) * expected_qty
        self.assertAlmostEqual(hist["exit_price"], exit_price)
        self.assertAlmostEqual(hist["slippage_usd"], 0.0003 * tp_trigger * expected_qty)
        self.assertAlmostEqual(hist["fee_usd"], expected_fee)
        self.assertAlmostEqual(hist["pnl_usd"], expected_gross - expected_fee)

    async def test_paper_price_monitor_closes_on_sl_tp(self):
        server.LIVE_MODE = False
        server.state["open_positions"] = {
            "ETHUSDT_TabA": {
                "symbol": "ETHUSDT",
                "side": "Short",
                "entry_price": 100.0,
                "qty": 1.0,
                "sl": 105.0,
                "tp": 95.0,
                "protection_mode": "paper_mark",
            }
        }
        server.latest_prices["ETHUSDT"] = 106.0
        server.latest_marks["ETHUSDT"] = 106.0
        with patch.object(server, "_close_position_unsafe", AsyncMock()) as close_pos:
            await server._check_local_position_exits_unsafe()
            close_pos.assert_awaited_once_with("ETHUSDT_TabA", 105.0, "SL")

        close_pos.reset_mock()
        server.latest_prices["ETHUSDT"] = 94.0
        server.latest_marks["ETHUSDT"] = 94.0
        with patch.object(server, "_close_position_unsafe", AsyncMock()) as close_pos:
            await server._check_local_position_exits_unsafe()
            close_pos.assert_awaited_once_with("ETHUSDT_TabA", 95.0, "TP")

    def test_protection_prices_from_entry_preserves_signal_distances(self):
        sl, tp = server._protection_prices_from_entry(
            "ETHUSDT", "Long", 101.0, 95.0, 110.0, 100.0
        )
        self.assertAlmostEqual(sl, 96.0)
        self.assertAlmostEqual(tp, 111.0)

    def test_planned_protection_prices_anchors_at_fill_not_signal_ep(self):
        """Live path: fill 101 with signal ep=100 keeps 6/9 distances, not signal absolute levels."""
        sl, tp = server._planned_protection_prices(
            "ETHUSDT", "Long", 101.0, 100.0, 95.0, 110.0, 100.0
        )
        self.assertAlmostEqual(sl, 96.0)
        self.assertAlmostEqual(tp, 111.0)
        sl_old, _ = server._planned_protection_prices(
            "ETHUSDT", "Long", 100.0, 100.0, 95.0, 110.0, 100.0
        )
        self.assertAlmostEqual(sl_old, 95.0)

    def test_planned_protection_local_skips_mark_nudge(self):
        """LOCAL_SLTP / paper: strategy distances only — no mark push on TP."""
        sl, tp = server._planned_protection_prices(
            "ALLOUSDT", "Long", 0.25319, 0.4006, 0.235467, 0.275344, 0.25319,
            use_local_protection=True,
        )
        self.assertAlmostEqual(sl, 0.235467, places=5)
        self.assertAlmostEqual(tp, 0.275344, places=5)

    def test_planned_protection_stale_mark_uses_entry_ref(self):
        """Exchange path: stale mark far from fill must not push TP to mark+nudge."""
        sl, tp = server._planned_protection_prices(
            "ALLOUSDT", "Long", 0.25319, 0.4006, 0.235467, 0.275344, 0.25319,
            use_local_protection=False,
        )
        self.assertAlmostEqual(sl, 0.235467, places=5)
        self.assertAlmostEqual(tp, 0.275344, places=5)

    def test_sltp_diff_pct_captures_mark_nudge(self):
        """Trusted mark within sanity band keeps SL/TP at strategy-anchored levels."""
        actual_sl, actual_tp = server._planned_protection_prices(
            "ETHUSDT", "Long", 100.0, 98.0, 95.0, 110.0, 100.0
        )
        sl_diff, tp_diff = server._sltp_diff_pct_from_entry(
            "ETHUSDT", "Long", 100.0, actual_sl, actual_tp, 95.0, 110.0, 100.0
        )
        self.assertAlmostEqual(actual_sl, 95.0)
        self.assertAlmostEqual(actual_tp, 110.0)
        self.assertAlmostEqual(sl_diff, 0.0)
        self.assertAlmostEqual(tp_diff, 0.0)

    def test_position_sltp_diff_fields_for_history(self):
        fields = server._position_sltp_diff_fields({
            "symbol": "ETHUSDT",
            "side": "Long",
            "entry_price": 101.0,
            "placed_sl": 96.0,
            "placed_tp": 111.0,
            "signal_sl": 95.0,
            "signal_tp": 110.0,
            "signal_entry_price": 100.0,
        })
        self.assertAlmostEqual(fields["sl_diff_pct"], 0.0)
        self.assertAlmostEqual(fields["tp_diff_pct"], 0.0)
        self.assertAlmostEqual(fields["placed_sl"], 96.0)

    def test_exit_target_slip_long_tp(self):
        info = server._exit_target_slip_from_fill(
            reason="TP",
            side="Long",
            entry_price=95000.0,
            exit_price=97350.0,
            placed_sl=93062.5,
            placed_tp=97375.0,
            sym="BTCUSDT",
        )
        self.assertIsNotNone(info)
        self.assertEqual(info["label"], "TP")
        self.assertAlmostEqual(info["slip_pct_str"], "-0.026%", places=2)
        self.assertFalse(info["slip_color"] == "green")

    def test_exit_target_slip_long_sl(self):
        info = server._exit_target_slip_from_fill(
            reason="SL",
            side="Long",
            entry_price=95000.0,
            exit_price=93010.0,
            placed_sl=93062.5,
            placed_tp=97375.0,
            sym="BTCUSDT",
        )
        self.assertIsNotNone(info)
        self.assertEqual(info["label"], "SL")
        self.assertIn("-0.055", info["slip_pct_str"])

    def test_exit_target_slip_short_tp_better_than_target(self):
        info = server._exit_target_slip_from_fill(
            reason="TP",
            side="Short",
            entry_price=0.14,
            exit_price=0.1247,
            placed_sl=0.15,
            placed_tp=0.12775,
            sym="OPNUSDT",
        )
        self.assertIsNotNone(info)
        self.assertEqual(info["label"], "TP")
        self.assertAlmostEqual(info["slip_pct_str"], "+2.179%", places=2)
        self.assertEqual(info["slip_color"], "green")

    def test_exit_target_slip_skips_manual(self):
        self.assertIsNone(server._exit_target_slip_from_fill(
            reason="ManualClose",
            side="Long",
            entry_price=100.0,
            exit_price=101.0,
            placed_sl=95.0,
            placed_tp=110.0,
            sym="ETHUSDT",
        ))

    def test_aggregate_binance_close_rows_groups_by_order(self):
        trades = [
            {"symbol": "ETHUSDT", "positionSide": "LONG", "orderId": 99, "side": "SELL",
             "qty": "1", "quoteQty": "100", "price": "100", "realizedPnl": "2", "commission": "-0.1", "time": 1000},
            {"symbol": "ETHUSDT", "positionSide": "LONG", "orderId": 99, "side": "SELL",
             "qty": "1", "quoteQty": "110", "price": "110", "realizedPnl": "1", "commission": "-0.05", "time": 1001},
            {"symbol": "ETHUSDT", "positionSide": "LONG", "orderId": 100, "side": "BUY",
             "qty": "1", "quoteQty": "90", "price": "90", "realizedPnl": "0", "commission": "-0.01", "time": 1002},
        ]
        rows = server._aggregate_binance_close_rows(trades)
        self.assertEqual(len(rows), 2)
        rows_by_oid = {int(r.get("close_order_id") or 0): r for r in rows}
        self.assertAlmostEqual(rows_by_oid[99]["pnl_usd"], 2.85)
        self.assertAlmostEqual(rows_by_oid[99]["exit_price"], 105.0)
        self.assertEqual(rows_by_oid[99]["side"], "Long")
        self.assertAlmostEqual(rows_by_oid[100]["pnl_usd"], -0.01)

    def test_estimate_entry_from_close_long(self):
        entry = server._estimate_entry_from_close("Long", 110.0, 10.0, 1.0)
        self.assertAlmostEqual(entry, 100.0)

    def test_enrich_history_entry_computes_missing_diff(self):
        row = server._enrich_history_entry({
            "symbol": "ETHUSDT",
            "side": "Long",
            "entry_price": 100.0,
            "signal_sl": 95.0,
            "signal_tp": 110.0,
            "signal_entry_price": 100.0,
            "placed_sl": 93.53,
            "placed_tp": 110.0,
        })
        self.assertLess(row["sl_diff_pct"], 0)

    async def test_mainnet_local_sltp_tab11_skips_exchange_algo_orders(self):
        """Local SL/TP mode: even Tab11 uses bot-managed SL/TP (no Binance algo legs)."""
        server.LIVE_MODE = True
        server.BINANCE_TESTNET = False
        server.LOCAL_SLTP = True
        server.state["sltp_mode"] = "local"
        server._circuit_breaker = False
        server.latest_prices["ETHUSDT"] = 100.0
        sig = {"side": "Long", "ep": 100.0, "sl": 96.0, "tp": 108.0}

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "record_error_event", AsyncMock()),
            patch.object(server, "record_sync_issue", AsyncMock()),
            patch.object(server, "_check_entry_quality", AsyncMock(return_value=(True, ""))),
            patch.object(server, "_live_position_qty", AsyncMock(return_value=1.0)),
            patch.object(server, "_verify_live_position_protection", AsyncMock(return_value=(True, ""))),
            patch.object(server, "_alert_position_protection_risk", AsyncMock()),
            patch.object(server.binance_live, "_sreq", AsyncMock(return_value={"markPrice": "100.0"})),
            patch.object(server.binance_live, "set_margin_type", AsyncMock()),
            patch.object(server.binance_live, "set_leverage", AsyncMock(return_value={"leverage": 5})),
            patch.object(
                server.binance_live,
                "place_market_order",
                AsyncMock(return_value={"orderId": 778, "avgPrice": "100.0", "executedQty": "1.0"}),
            ),
            patch.object(server.binance_live, "place_stop_loss", AsyncMock()) as place_sl,
            patch.object(server.binance_live, "place_take_profit", AsyncMock()) as place_tp,
        ):
            await server._execute_entry_unsafe("ETHUSDT", sig, "Tab11")

        place_sl.assert_not_awaited()
        place_tp.assert_not_awaited()
        pos = server.state["open_positions"]["ETHUSDT_Tab11"]
        self.assertEqual(pos["protection_status"], "local")
        self.assertEqual(pos["protection_mode"], "local")
        self.assertEqual(pos["protection_reason"], "mainnet_local_sl_tp")
        self.assertIsNone(pos["sl_order_id"])
        self.assertIsNone(pos["tp_order_id"])

    async def test_tab11_entry_uses_exchange_protection_when_local_sltp_off(self):
        server.LIVE_MODE = True
        server.BINANCE_TESTNET = True
        server.state["sltp_mode"] = "binance"
        server._circuit_breaker = False
        server.latest_prices["ETHUSDT"] = 100.0
        sig = {"side": "Long", "ep": 100.0, "sl": 96.0, "tp": 108.0}

        async def fake_sreq(_client, _method, path, params=None):
            if path == "/fapi/v1/premiumIndex":
                return {"markPrice": "100.0"}
            if path == "/fapi/v1/openAlgoOrders":
                return [
                    {"algoId": 301, "positionSide": "LONG"},
                    {"algoId": 302, "positionSide": "LONG"},
                ]
            return {}

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "record_error_event", AsyncMock()),
            patch.object(server, "record_sync_issue", AsyncMock()),
            patch.object(server, "_check_entry_quality", AsyncMock(return_value=(True, ""))),
            patch.object(server, "_open_algo_order_count", AsyncMock(return_value=0)) as open_algo_count,
            patch.object(server, "_live_position_qty", AsyncMock(return_value=1.0)),
            patch.object(server, "_verify_live_position_protection", AsyncMock(return_value=(True, ""))),
            patch.object(server.binance_live, "_sreq", AsyncMock(side_effect=fake_sreq)),
            patch.object(server.binance_live, "set_margin_type", AsyncMock()),
            patch.object(server.binance_live, "set_leverage", AsyncMock(return_value={"leverage": 5})),
            patch.object(
                server.binance_live,
                "place_market_order",
                AsyncMock(return_value={"orderId": 777, "avgPrice": "100.0", "executedQty": "1.0"}),
            ),
            patch.object(server.binance_live, "place_stop_loss", AsyncMock(return_value={"algoId": 301})) as place_sl,
            patch.object(server.binance_live, "place_take_profit", AsyncMock(return_value={"algoId": 302})) as place_tp,
        ):
            await server._execute_entry_unsafe("ETHUSDT", sig, "Tab11")

        open_algo_count.assert_awaited_once()
        place_sl.assert_awaited_once()
        place_tp.assert_awaited_once()
        pos = server.state["open_positions"]["ETHUSDT_Tab11"]
        self.assertEqual(pos["protection_status"], "exchange")
        self.assertNotIn("protection_mode", pos)
        self.assertEqual(pos["sl_order_id"], 301)
        self.assertEqual(pos["tp_order_id"], 302)

    def test_planned_protection_validation_rejects_wrong_side(self):
        ok, reason = server._validate_planned_protection(
            "ETHUSDT",
            "Long",
            qty=1.0,
            entry_px=100.0,
            sl_price=101.0,
            tp_price=102.0,
            mark_px=100.0,
        )

        self.assertFalse(ok)
        self.assertIn("wrong side", reason)

    def test_planned_protection_allows_risk_at_max_sl_cap(self):
        entry = 0.0105
        sl = entry * (1 + server.MAX_SL_PCT)
        ok, reason = server._validate_planned_protection(
            "SWARMSUSDT",
            "Short",
            qty=949.0,
            entry_px=entry,
            sl_price=sl,
            tp_price=entry * (1 - server.MAX_SL_PCT * 2),
            mark_px=entry,
            use_local_protection=True,
        )
        self.assertTrue(ok, reason)

    def test_planned_protection_allows_risk_slightly_above_cap_from_rounding(self):
        entry = 0.0105
        sl = entry * (1 + server.MAX_SL_PCT + 0.00005)
        ok, reason = server._validate_planned_protection(
            "SWARMSUSDT",
            "Short",
            qty=949.0,
            entry_px=entry,
            sl_price=sl,
            tp_price=entry * 0.0096,
            mark_px=entry,
            use_local_protection=True,
        )
        self.assertTrue(ok, reason)

    def test_planned_protection_rejects_risk_clearly_above_cap(self):
        entry = 0.0105
        sl = entry * (1 + server.MAX_SL_PCT + 0.002)
        ok, reason = server._validate_planned_protection(
            "SWARMSUSDT",
            "Short",
            qty=949.0,
            entry_px=entry,
            sl_price=sl,
            tp_price=entry * 0.0096,
            mark_px=entry,
            use_local_protection=True,
        )
        self.assertFalse(ok)
        self.assertIn("exceeds max", reason)

    def test_health_snapshot_reports_watchdog_heartbeats(self):
        server.LIVE_MODE = False
        server._last_price_ws_ok_at = "2020-01-01T00:00:00+00:00"
        server._last_scheduler_ok_at = "2020-01-01T00:00:00+00:00"
        server._last_watchdog_ok_at = "2020-01-01T00:00:00+00:00"

        health = server._health_snapshot()

        self.assertEqual(health["status"], "warning")
        self.assertIn("price websocket stale", health["reasons"])
        self.assertIn("scheduler heartbeat stale", health["reasons"])
        self.assertIn("watchdog heartbeat stale", health["reasons"])

    def test_effective_sltp_mode_uses_state_then_env(self):
        server.state["sltp_mode"] = "hybrid"
        self.assertEqual(server._effective_sltp_mode(), "hybrid")
        self.assertFalse(server._effective_local_sltp())
        server.state["sltp_mode"] = "local"
        self.assertTrue(server._effective_local_sltp())
        server.state.pop("sltp_mode", None)
        server.state["local_sltp"] = True
        self.assertEqual(server._effective_sltp_mode(), "local")
        server.state.pop("local_sltp", None)
        server.SLTP_MODE = "binance_fallback"
        self.assertEqual(server._effective_sltp_mode(), "binance_fallback")
        server.SLTP_MODE = "local"
        self.assertTrue(server._effective_local_sltp())

    def test_health_snapshot_ignores_mainnet_local_sltp_positions(self):
        now = datetime.now(timezone.utc).isoformat()
        server.LIVE_MODE = True
        server.LOCAL_SLTP = True
        server.state["sltp_mode"] = "local"
        server.BINANCE_TESTNET = False
        server._uds_connected = True
        server._last_price_ws_ok_at = now
        server._last_scheduler_ok_at = now
        server._last_watchdog_ok_at = now
        server._last_sync_ok_at = now
        server._last_exchange_account_ok_at = now
        server.state["open_positions"] = {
            "BTCUSDT_Tab11": {
                "symbol": "BTCUSDT",
                "tab": "Tab11",
                "side": "Long",
                "protection_mode": "local",
                "protection_reason": server._MAINNET_LOCAL_SLTP_REASON,
            }
        }

        health = server._health_snapshot()

        self.assertEqual(health["protection_risk_count"], 0)
        self.assertEqual(health["warning_protection_count"], 0)
        self.assertNotIn("local/pending protection warning", " ".join(health["reasons"]))

    def test_dashboard_pnl_summary_paper_includes_unrealized(self):
        server.LIVE_MODE = False
        server.state["history"] = [
            {"tab": "TabA", "pnl_usd": 12.5},
            {"tab": "TabB", "pnl_usd": -2.0},
        ]
        server.state["unrealized_pnls"] = {"TabA": 3.0, "TabB": -1.5}

        summary = server._dashboard_pnl_summary()

        self.assertEqual(summary["source"], "bot_history_plus_open_state")
        self.assertAlmostEqual(summary["all"]["realized"], 10.5)
        self.assertAlmostEqual(summary["all"]["unrealized"], 1.5)
        self.assertAlmostEqual(summary["all"]["total"], 12.0)
        self.assertAlmostEqual(summary["per_tab"]["TabA"]["total"], 15.5)

    def test_accumulate_gross_breakdown_splits_profit_and_loss(self):
        bucket = {"gross_profit": 0.0, "gross_loss": 0.0}
        server._accumulate_gross_breakdown(bucket, 12.5)
        server._accumulate_gross_breakdown(bucket, -3.25)
        server._accumulate_gross_breakdown(bucket, 0.0)
        self.assertAlmostEqual(bucket["gross_profit"], 12.5)
        self.assertAlmostEqual(bucket["gross_loss"], 3.25)

    def test_attribute_income_to_tab_matches_close_exit_time(self):
        exit_iso = datetime.now(timezone.utc).isoformat()
        server.state["history"] = [
            {
                "tab": "Tab3",
                "symbol": "ETHUSDT",
                "exit_time": exit_iso,
                "position_side": "LONG",
            }
        ]
        exit_ms = server._dt_to_ms(exit_iso)
        tab = server._attribute_income_to_tab("ETHUSDT", exit_ms + 30_000, position_side="LONG")
        self.assertEqual(tab, "Tab3")
        unknown = server._attribute_income_to_tab("BTCUSDT", exit_ms)
        self.assertEqual(unknown, "Recovered")

    def test_attribute_income_to_tab_uses_registry_and_position_side(self):
        exit_iso = datetime.now(timezone.utc).isoformat()
        exit_ms = server._dt_to_ms(exit_iso)
        server.state["position_registry"] = {
            "BTCUSDT_Tab8": {
                "tab": "Tab8",
                "symbol": "BTCUSDT",
                "side": "Long",
                "position_side": "LONG",
                "status": "closed",
                "closed_at": exit_iso,
            },
            "BTCUSDT_Tab11": {
                "tab": "Tab11",
                "symbol": "BTCUSDT",
                "side": "Long",
                "position_side": "LONG",
                "status": "closed",
                "closed_at": exit_iso,
            },
        }
        server.state["history"] = [
            {
                "tab": "Tab11",
                "symbol": "BTCUSDT",
                "position_side": "LONG",
                "exit_time": exit_iso,
            },
            {
                "tab": "Tab8",
                "symbol": "BTCUSDT",
                "position_side": "LONG",
                "exit_time": exit_iso,
            },
        ]
        server.state["position_registry"]["BTCUSDT_Tab8"]["closed_at"] = exit_iso
        tab8_ms = server._dt_to_ms(exit_iso) - 1_000
        server.state["position_registry"]["BTCUSDT_Tab8"]["closed_at"] = server._ms_to_iso(tab8_ms)
        tab = server._attribute_income_to_tab(
            "BTCUSDT",
            tab8_ms + 500,
            position_side="LONG",
        )
        self.assertEqual(tab, "Tab8")

    def test_is_winning_trade_strict_positive(self):
        self.assertTrue(server._is_winning_trade(0.01))
        self.assertFalse(server._is_winning_trade(0.0))
        self.assertFalse(server._is_winning_trade(-0.01))

    def test_tab_stats_rebuild_and_record_close(self):
        server.state["history"] = [
            {"tab": "Tab3", "pnl_usd": 2.0},
            {"tab": "Tab3", "pnl_usd": -1.0},
            {"tab": "Tab8", "pnl_usd": 0.0},
        ]
        server._rebuild_tab_stats_from_history()
        tab3 = server.state["tab_stats"]["Tab3"]
        self.assertEqual(tab3["trades"], 2)
        self.assertEqual(tab3["wins"], 1)
        self.assertAlmostEqual(tab3["best"], 2.0)
        self.assertAlmostEqual(tab3["worst"], -1.0)
        tab8 = server.state["tab_stats"]["Tab8"]
        self.assertEqual(tab8["trades"], 1)
        self.assertEqual(tab8["wins"], 0)

        server._record_tab_stats_close("Tab3", 5.0)
        tab3 = server.state["tab_stats"]["Tab3"]
        self.assertEqual(tab3["trades"], 3)
        self.assertEqual(tab3["wins"], 2)
        self.assertAlmostEqual(tab3["best"], 5.0)

    def test_dashboard_stats_meta_live(self):
        server.LIVE_MODE = True
        server._BINANCE_CLOSE_HISTORY_CACHE = []
        meta = server._dashboard_stats_meta()
        self.assertFalse(meta["close_history_enabled"])
        self.assertEqual(meta["trade_counts"], "tab_stats")
        self.assertEqual(meta["recent_trades_table"], "bot_history")
        self.assertEqual(meta["live_realized_dollars"], "binance_tab_income")
        self.assertGreaterEqual(meta["close_history_days"], 7)

    def test_refresh_binance_close_history_disabled(self):
        server.LIVE_MODE = True
        with patch.object(server, "BINANCE_CLOSE_HISTORY_ENABLED", False):
            self.assertFalse(server._binance_close_history_enabled())
            row_count = asyncio.run(server._refresh_binance_close_history(force=True))
        self.assertEqual(row_count, 0)

    def test_history_rows_as_api_trades(self):
        server.state["history"] = [
            {
                "symbol": "BTCUSDT",
                "side": "Long",
                "position_side": "LONG",
                "exit_time": "2026-01-01T12:00:00Z",
                "exit_price": 100.0,
                "qty": 0.1,
                "pnl_usd": 1.5,
                "fee_usd": 0.02,
            },
        ]
        rows = server._history_rows_as_api_trades(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "BTCUSDT")
        self.assertEqual(rows[0]["side"], "BUY")

    def test_income_tran_high_water_dedup(self):
        inc = {"income_tran_high_water": 100, "seen_tran_ids": []}
        seen: set[int] = set()
        self.assertTrue(server._income_tran_already_applied(inc, 50, seen))
        self.assertFalse(server._income_tran_already_applied(inc, 101, seen))
        server._mark_income_tran_applied(inc, 101, seen)
        self.assertTrue(server._income_tran_already_applied(inc, 101, seen))

    def test_rebuild_tab_stats_from_binance_closes(self):
        server.LIVE_MODE = True
        with patch.object(server, "BINANCE_CLOSE_HISTORY_ENABLED", True):
            server._BINANCE_CLOSE_HISTORY_CACHE = [
                {"tab": "Tab3", "pnl_usd": 2.0, "exit_time": "2026-01-01T00:00:00Z"},
                {"tab": "Tab3", "pnl_usd": -1.0, "exit_time": "2026-01-02T00:00:00Z"},
                {"tab": "Recovered", "pnl_usd": 0.5, "exit_time": "2026-01-03T00:00:00Z"},
            ]
            server._rebuild_tab_stats_from_binance_closes()
            tab3 = server.state["tab_stats"]["Tab3"]
            self.assertEqual(tab3["trades"], 2)
            self.assertEqual(tab3["wins"], 1)
            self.assertEqual(server.state["tab_stats"]["Recovered"]["trades"], 1)

    def test_rebuild_tab_stats_for_tab_after_history_change(self):
        server.state["history"] = [
            {"tab": "Tab5", "pnl_usd": 3.0},
            {"tab": "Tab5", "pnl_usd": 1.0},
        ]
        server._rebuild_tab_stats_for_tab("Tab5")
        row = server.state["tab_stats"]["Tab5"]
        self.assertEqual(row["trades"], 2)
        self.assertEqual(row["wins"], 2)

    def test_effective_tab_gross_falls_back_to_history(self):
        server.state["history"] = [
            {"tab": "Tab15", "pnl_usd": 2.0},
            {"tab": "Tab15", "pnl_usd": -1.0},
        ]
        tab_inc = {
            "Tab15": {"gross_profit": 0.0, "gross_loss": 0.001},
            "Recovered": {"gross_profit": 40.0, "gross_loss": 40.0},
        }
        gp, gl = server._effective_tab_gross("Tab15", tab_inc)
        self.assertAlmostEqual(gp, 2.0)
        self.assertAlmostEqual(gl, 1.0)
        rgp, rgl = server._effective_tab_gross("Recovered", tab_inc)
        self.assertAlmostEqual(rgp, 40.0)
        self.assertAlmostEqual(rgl, 40.0)

    def test_dashboard_equity_close_series_sorted(self):
        server.LIVE_MODE = True
        with patch.object(server, "BINANCE_CLOSE_HISTORY_ENABLED", True):
            server._BINANCE_CLOSE_HISTORY_CACHE = [
                {"tab": "Tab1", "pnl_usd": 1.0, "exit_time": "2026-01-02T00:00:00Z", "symbol": "BTCUSDT"},
                {"tab": "Tab1", "pnl_usd": 2.0, "exit_time": "2026-01-01T00:00:00Z", "symbol": "ETHUSDT"},
            ]
            series = server._dashboard_equity_close_series()
        self.assertGreaterEqual(len(series), 3)
        self.assertAlmostEqual(series[-1]["cumulative"], 3.0)

    def test_equity_curve_baseline_from_first_snapshot(self):
        server.LIVE_MODE = True
        server.state["binance_income"] = {
            "realized_pnl": 20.0,
            "commission": -1.0,
            "funding": 2.0,
        }
        server.state["binance_tab_income"] = {
            "Tab1": {"gross_profit": 30.0, "gross_loss": 10.0},
        }
        server.state["equity_snapshots"] = [
            {
                "ts_ms": 1_000_000,
                "account_realized": 26.68,
                "strategy_realized": 20.0,
            },
        ]
        base = server._dashboard_equity_curve_baseline()
        self.assertAlmostEqual(base["account"], 26.68)
        self.assertAlmostEqual(base["strategy"], 20.0)
        self.assertAlmostEqual(server._strategy_realized_total(), 20.0)

    def test_apply_income_record_tracks_gross_and_tab(self):
        server.state["history"] = []
        server.state["binance_income"] = {
            "realized_pnl": 0.0,
            "commission": 0.0,
            "funding": 0.0,
            "last_ts": 0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "seen_tran_ids": [],
        }
        server.state["binance_tab_income"] = {"Tab1": server._empty_tab_income()}
        inc = server.state["binance_income"]
        tabs = server.state["binance_tab_income"]
        seen = set()
        server._apply_income_record(
            {"tranId": 1, "incomeType": "REALIZED_PNL", "income": "5", "time": 1000, "symbol": "BTCUSDT"},
            inc=inc,
            tabs=tabs,
            seen=seen,
        )
        server._apply_income_record(
            {"tranId": 2, "incomeType": "COMMISSION", "income": "-0.5", "time": 1001, "symbol": "BTCUSDT"},
            inc=inc,
            tabs=tabs,
            seen=seen,
        )
        self.assertAlmostEqual(inc["realized_pnl"], 5.0)
        self.assertAlmostEqual(inc["commission"], -0.5)
        self.assertAlmostEqual(inc["gross_profit"], 5.0)
        self.assertAlmostEqual(inc["gross_loss"], 0.5)
        self.assertAlmostEqual(tabs["Recovered"]["gross_profit"], 5.0)
        self.assertAlmostEqual(tabs["Recovered"]["gross_loss"], 0.5)

    def test_recalculate_unrealized_pnls_live_splits_binance_leg(self):
        server.LIVE_MODE = True
        server.latest_marks = {}
        server.latest_prices = {}
        server.exchange_account = {
            "positions": [
                {"symbol": "BTCUSDT", "side": "Long", "unrealizedProfit": 10.0},
            ]
        }
        server.state["open_positions"] = {
            "BTCUSDT_Tab1": {
                "symbol": "BTCUSDT",
                "side": "Long",
                "qty": 0.01,
                "tab": "Tab1",
                "entry_price": 100.0,
            },
            "BTCUSDT_Tab2": {
                "symbol": "BTCUSDT",
                "side": "Long",
                "qty": 0.03,
                "tab": "Tab2",
                "entry_price": 110.0,
            },
        }
        server.state["unrealized_pnls"]["Tab1"] = 0.0
        server.state["unrealized_pnls"]["Tab2"] = 0.0

        server._recalculate_unrealized_pnls()

        self.assertAlmostEqual(server.state["unrealized_pnls"]["Tab1"], 2.5)
        self.assertAlmostEqual(server.state["unrealized_pnls"]["Tab2"], 7.5)

    def test_recalculate_unrealized_pnls_live_uses_mark_when_available(self):
        server.LIVE_MODE = True
        server.latest_marks = {"BTCUSDT": 105.0}
        server.latest_prices = {}
        server.exchange_account = {
            "positions": [
                {
                    "symbol": "BTCUSDT",
                    "side": "Long",
                    "positionAmt": 0.04,
                    "entryPrice": 100.0,
                    "markPrice": 100.0,
                    "unrealizedProfit": 0.0,
                },
            ],
            "walletBalance": 1000.0,
            "unrealizedProfit": 0.0,
            "marginBalance": 1000.0,
        }
        server.state["open_positions"] = {
            "BTCUSDT_Tab1": {
                "symbol": "BTCUSDT",
                "side": "Long",
                "qty": 0.01,
                "tab": "Tab1",
                "entry_price": 100.0,
            },
            "BTCUSDT_Tab2": {
                "symbol": "BTCUSDT",
                "side": "Long",
                "qty": 0.03,
                "tab": "Tab2",
                "entry_price": 110.0,
            },
        }
        server._refresh_exchange_account_marks()
        server._recalculate_unrealized_pnls()
        self.assertAlmostEqual(server.exchange_account["unrealizedProfit"], 0.2)
        self.assertAlmostEqual(server.state["unrealized_pnls"]["Tab1"], 0.05)
        self.assertAlmostEqual(server.state["unrealized_pnls"]["Tab2"], -0.15)

    def test_dashboard_pnl_summary_live_includes_account_and_strategy(self):
        server.LIVE_MODE = True
        server.state["history"] = [
            {"tab": "TabA", "pnl_usd": 10.0},
            {"tab": "TabB", "pnl_usd": -2.5},
        ]
        server.state["unrealized_pnls"] = {"TabA": 1.0, "TabB": -0.5, "Recovered": 99.0}
        server.state["binance_income"] = {
            "realized_pnl": -20.0,
            "commission": -1.0,
            "funding": -0.5,
            "last_ts": 0,
            "gross_profit": 15.0,
            "gross_loss": 23.5,
            "gross_rebuilt": True,
        }
        server.state["binance_tab_income"] = {
            "TabA": {"gross_profit": 10.0, "gross_loss": 0.0},
            "TabB": {"gross_profit": 0.0, "gross_loss": 2.5},
        }
        server.exchange_account = {
            "positions": [
                {"symbol": "ETHUSDT", "positionAmt": "0.1", "unRealizedProfit": "3.0"},
            ]
        }

        summary = server._dashboard_pnl_summary()

        self.assertEqual(summary["source"], "strategy_and_account")
        self.assertAlmostEqual(summary["strategy"]["realized"], 7.5)
        self.assertAlmostEqual(summary["strategy"]["unrealized"], 0.5)
        self.assertAlmostEqual(summary["account"]["realized"], -21.5)
        self.assertAlmostEqual(summary["account"]["gross_profit"], 15.0)
        self.assertAlmostEqual(summary["account"]["gross_loss"], 23.5)
        self.assertAlmostEqual(summary["per_tab"]["TabA"]["gross_profit"], 10.0)
        self.assertAlmostEqual(summary["per_tab"]["TabB"]["gross_loss"], 2.5)
        self.assertAlmostEqual(summary["account"]["unrealized"], 3.0)
        self.assertAlmostEqual(summary["account"]["total"], -18.5)

    def test_apply_income_gross_includes_funding_fee(self):
        inc = {"gross_profit": 0.0, "gross_loss": 0.0}
        tabs: dict = {}
        seen: set[int] = set()
        server._apply_income_gross_record(
            {
                "tranId": 10,
                "incomeType": "REALIZED_PNL",
                "income": "5",
                "time": 1000,
                "symbol": "BTCUSDT",
            },
            inc=inc,
            tabs=tabs,
            seen=seen,
        )
        server._apply_income_gross_record(
            {
                "tranId": 11,
                "incomeType": "COMMISSION",
                "income": "-0.4",
                "time": 1001,
                "symbol": "BTCUSDT",
            },
            inc=inc,
            tabs=tabs,
            seen=seen,
        )
        server._apply_income_gross_record(
            {
                "tranId": 12,
                "incomeType": "FUNDING_FEE",
                "income": "-0.1",
                "time": 1002,
                "symbol": "BTCUSDT",
            },
            inc=inc,
            tabs=tabs,
            seen=seen,
        )
        self.assertAlmostEqual(inc["gross_profit"], 5.0)
        self.assertAlmostEqual(inc["gross_loss"], 0.5)
        self.assertAlmostEqual(server._binance_gross_net(inc["gross_profit"], inc["gross_loss"]), 4.5)

    def test_summarize_income_records_matches_binance_today_pnl(self):
        records = [
            {"incomeType": "REALIZED_PNL", "income": "10.5"},
            {"incomeType": "COMMISSION", "income": "-0.25"},
            {"incomeType": "FUNDING_FEE", "income": "-0.10"},
            {"incomeType": "TRANSFER", "income": "1000"},
        ]
        totals = server._summarize_income_records(records)
        self.assertAlmostEqual(totals["realized_pnl"], 10.5)
        self.assertAlmostEqual(totals["commission"], -0.25)
        self.assertAlmostEqual(totals["funding"], -0.10)
        self.assertAlmostEqual(totals["net"], 10.15)

    def test_group_income_by_utc_day(self):
        records = [
            {"incomeType": "REALIZED_PNL", "income": "10", "time": 1_746_000_000_000},
            {"incomeType": "COMMISSION", "income": "-1", "time": 1_746_000_000_000},
            {"incomeType": "REALIZED_PNL", "income": "5", "time": 1_746_086_400_000},
        ]
        by_day = server._group_income_by_utc_day(records)
        self.assertEqual(len(by_day), 2)
        day1 = server._utc_day_str_from_ms(1_746_000_000_000)
        day2 = server._utc_day_str_from_ms(1_746_086_400_000)
        self.assertAlmostEqual(by_day[day1]["net"], 9.0)
        self.assertAlmostEqual(by_day[day2]["net"], 5.0)

    def test_build_daily_profit_30d_series_length_and_zeros(self):
        fixed = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)
        by_day = {
            "2026-06-04": {"net": 3.5, "realized_pnl": 4.0, "commission": -0.5, "funding": 0.0},
        }
        series = server._build_daily_profit_30d_series(by_day, fixed)
        self.assertEqual(len(series), 30)
        self.assertEqual(series[-1]["date_utc"], "2026-06-04")
        self.assertAlmostEqual(series[-1]["net"], 3.5)
        self.assertAlmostEqual(series[0]["net"], 0.0)

    def test_dashboard_daily_profit_30d_live_only(self):
        server.LIVE_MODE = True
        server._daily_profit_30d = [
            {"date_utc": "2026-06-04", "net": 1.0, "realized_pnl": 1.0, "commission": 0.0, "funding": 0.0},
        ]
        out = server._dashboard_daily_profit_30d()
        self.assertIsNotNone(out)
        self.assertEqual(len(out), 1)
        server.LIVE_MODE = False
        self.assertIsNone(server._dashboard_daily_profit_30d())

    def test_dashboard_pnl_summary_includes_today_profit(self):
        server.LIVE_MODE = True
        server._today_binance_profit = {
            "net": 4.5,
            "realized_pnl": 5.0,
            "commission": -0.3,
            "funding": -0.2,
            "date_utc": "2026-05-25",
        }
        server.state["binance_income"] = {
            "realized_pnl": 0.0,
            "commission": 0.0,
            "funding": 0.0,
            "last_ts": 0,
        }
        server.exchange_account = {"positions": []}

        summary = server._dashboard_pnl_summary()
        today = summary["account"]["today"]
        self.assertIsNotNone(today)
        self.assertAlmostEqual(today["net"], 4.5)
        self.assertEqual(today["date_utc"], "2026-05-25")

    def test_match_close_trades_prefers_order_id(self):
        trades = [
            {"id": 10, "orderId": 1, "positionSide": "LONG", "side": "SELL", "qty": "2", "time": 1000,
             "realizedPnl": "-1", "commission": "-0.01", "quoteQty": "20", "price": "10"},
            {"id": 11, "orderId": 2, "positionSide": "LONG", "side": "SELL", "qty": "5", "time": 1005,
             "realizedPnl": "-2", "commission": "-0.02", "quoteQty": "50", "price": "10"},
        ]
        matched = server._match_close_trades(
            trades,
            pos_side="LONG",
            close_side="SELL",
            qty=5.0,
            exit_ms=1005,
            order_id=2,
        )
        self.assertEqual(len(matched), 1)
        self.assertEqual(int(matched[0]["orderId"]), 2)

    def test_match_close_trades_strict_order_no_fallback(self):
        """When order_id is known but not in trades yet, do not grab a nearby fill."""
        trades = [
            {"id": 99, "orderId": 9, "positionSide": "LONG", "side": "SELL", "qty": "20", "time": 1005,
             "realizedPnl": "0.07", "commission": "0", "quoteQty": "0.35", "price": "0.017"},
        ]
        matched = server._match_close_trades(
            trades,
            pos_side="LONG",
            close_side="SELL",
            qty=567.0,
            exit_ms=1005,
            order_id=12345,
            strict_order=True,
        )
        self.assertEqual(matched, [])

    def test_match_close_trades_excludes_consumed_trade_ids(self):
        trades = [
            {"id": 1, "orderId": 2, "positionSide": "LONG", "side": "SELL", "qty": "5", "time": 1005,
             "realizedPnl": "2", "commission": "-0.02", "quoteQty": "50", "price": "10"},
        ]
        matched = server._match_close_trades(
            trades,
            pos_side="LONG",
            close_side="SELL",
            qty=5.0,
            exit_ms=1005,
            order_id=2,
            exclude_trade_ids={1},
        )
        self.assertEqual(matched, [])

    async def test_apply_history_pnl_update_rejects_undersized_reconcile(self):
        entry = {
            "tab": "Tab15",
            "symbol": "BLUAIUSDT",
            "qty": 567.0,
            "close_order_id": 999,
            "pnl_usd": 1.95,
        }
        summary = {
            "net_pnl": 0.07,
            "realized_pnl": 0.07,
            "commission": 0.0,
            "exit_price": 0.021,
            "trade_qty": 20.0,
            "trade_count": 1,
            "trade_ids": [42],
        }
        changed = await server._apply_history_pnl_update(
            entry, summary, 1.95, label="BLUAIUSDT LONG Tab15"
        )
        self.assertFalse(changed)
        self.assertAlmostEqual(entry["pnl_usd"], 1.95)

    def test_fill_net_pnl_adds_commission(self):
        self.assertAlmostEqual(server._fill_net_pnl(1.5, -0.05), 1.45)
        self.assertIsNone(server._fill_net_pnl(None, -0.05))

    def test_order_fill_commission_usd_usdt_only(self):
        self.assertAlmostEqual(
            server._order_fill_commission_usd({"n": "-0.12", "N": "USDT"}),
            -0.12,
        )
        self.assertIsNone(server._order_fill_commission_usd({"n": "-0.12", "N": "BNB"}))

    def test_order_commission_parts_bnb_converts_fee_display(self):
        server.latest_prices["BNBUSDT"] = 600.0
        usdt_part, fee_part = server._order_commission_parts({"n": "-0.00001", "N": "BNB"})
        self.assertIsNone(usdt_part)
        self.assertAlmostEqual(fee_part, 0.006)

    def test_summarize_close_trades_net_includes_commission(self):
        trades = [
            {"qty": "2", "quoteQty": "20", "price": "10", "realizedPnl": "1.5", "commission": "-0.05"},
            {"qty": "3", "quoteQty": "30", "price": "10", "realizedPnl": "0.5", "commission": "-0.03"},
        ]
        summary = server._summarize_close_trades(trades)
        self.assertAlmostEqual(summary["realized_pnl"], 2.0)
        self.assertAlmostEqual(summary["commission"], -0.08)
        self.assertAlmostEqual(summary["fee_usd"], 0.08)
        self.assertAlmostEqual(summary["net_pnl"], 1.92)
        self.assertAlmostEqual(summary["exit_price"], 10.0)

    def test_summarize_close_trades_bnb_fee_does_not_change_usdt_net(self):
        server.latest_prices["BNBUSDT"] = 500.0
        trades = [{
            "qty": "60", "quoteQty": "9.1", "price": "0.1518",
            "realizedPnl": "0.888", "commission": "-0.000012", "commissionAsset": "BNB",
        }]
        summary = server._summarize_close_trades(trades)
        self.assertAlmostEqual(summary["realized_pnl"], 0.888)
        self.assertAlmostEqual(summary["commission"], 0.0)
        self.assertAlmostEqual(summary["net_pnl"], 0.888)
        self.assertAlmostEqual(summary["fee_usd"], 0.006)

    async def test_fetch_close_pnl_income_commission_fallback(self):
        server.latest_prices["BNBUSDT"] = 600.0
        exit_iso = "2026-06-06T16:51:30+00:00"
        exit_ms = server._dt_to_ms(exit_iso)

        async def fake_trades(*_a, **_k):
            return [{
                "id": 1, "orderId": 99, "positionSide": "SHORT", "side": "BUY",
                "qty": "60", "quoteQty": "9.1", "price": "0.1518", "time": exit_ms,
                "realizedPnl": "0.888", "commission": "0", "commissionAsset": "BNB",
            }]

        async def fake_income(*_a, **_k):
            return [{"income": "-0.00001", "asset": "BNB", "time": exit_ms, "incomeType": "COMMISSION"}]

        server._http_client = object()
        with patch.object(server.binance_live, "get_account_trades", side_effect=fake_trades), \
             patch.object(server.binance_live, "get_income", side_effect=fake_income):
            summary = await server._fetch_close_pnl_from_trades(
                "OPNUSDT", "SHORT", "Short", 60.0, exit_iso, order_id=99, ignore_entry_window=True,
            )
        self.assertIsNotNone(summary)
        self.assertAlmostEqual(summary["realized_pnl"], 0.888)
        self.assertAlmostEqual(summary["fee_usd"], 0.006)

    async def test_close_all_removes_stale_state_when_exchange_flat(self):
        server.LIVE_MODE = True
        server._BINANCE_RATE_LIMIT_UNTIL_MS = 0
        server._BINANCE_RATE_LIMIT_REASON = ""
        server._BINANCE_RATE_LIMIT_ALERT_SENT = False
        server.state["open_positions"] = {
            "ETHUSDT_Tab1": {
                "tab": "Tab1",
                "symbol": "ETHUSDT",
                "side": "Long",
                "position_side": "LONG",
                "entry_price": 3000.0,
                "sl": 2900.0,
                "tp": 3200.0,
                "qty": 0.01,
                "entry_time": "2026-05-23T08:00:00",
            }
        }
        live_qty_cache = {("ETHUSDT", "LONG"): 0.0}
        rate_exc = httpx.HTTPStatusError(
            "418",
            request=httpx.Request("GET", "https://fapi.binance.com/fapi/v1/order"),
            response=httpx.Response(418, text='{"code":-1003,"msg":"Way too many requests"}'),
        )

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "_reconcile_pnl_from_binance", AsyncMock()),
            patch.object(server, "trigger_income_sync", AsyncMock()),
            patch.object(server, "_sweep_algo_orders_for", AsyncMock()),
            patch.object(server.binance_live, "place_market_order", AsyncMock(side_effect=rate_exc)),
        ):
            await server._close_position_unsafe(
                "ETHUSDT_Tab1",
                3000.0,
                "Manual",
                live_qty_cache=live_qty_cache,
            )

        self.assertNotIn("ETHUSDT_Tab1", server.state["open_positions"])
        self.assertEqual(server.state["history"][-1]["reason"], "Manual")

    async def test_emergency_close_batch_one_market_order_per_leg(self):
        server.LIVE_MODE = True
        server._BINANCE_RATE_LIMIT_UNTIL_MS = 0
        server._BINANCE_RATE_LIMIT_REASON = ""
        server._BINANCE_RATE_LIMIT_ALERT_SENT = False
        server.state["open_positions"] = {
            "BTCUSDT_Tab1": {
                "tab": "Tab1",
                "symbol": "BTCUSDT",
                "side": "Long",
                "position_side": "LONG",
                "entry_price": 60000.0,
                "sl": 59000.0,
                "tp": 62000.0,
                "qty": 0.01,
                "entry_time": "2026-06-05T08:00:00",
            },
            "BTCUSDT_TabA": {
                "tab": "TabA",
                "symbol": "BTCUSDT",
                "side": "Long",
                "position_side": "LONG",
                "entry_price": 60100.0,
                "sl": 59100.0,
                "tp": 62100.0,
                "qty": 0.02,
                "entry_time": "2026-06-05T09:00:00",
            },
            "ETHUSDT_TabB": {
                "tab": "TabB",
                "symbol": "ETHUSDT",
                "side": "Short",
                "position_side": "SHORT",
                "entry_price": 3000.0,
                "sl": 3100.0,
                "tp": 2800.0,
                "qty": 0.05,
                "entry_time": "2026-06-05T08:30:00",
            },
        }
        live_cache = {
            ("BTCUSDT", "LONG"): 0.03,
            ("ETHUSDT", "SHORT"): 0.05,
        }

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "_reconcile_pnl_from_binance", AsyncMock()),
            patch.object(server, "trigger_income_sync", AsyncMock()),
            patch.object(server, "_sweep_algo_orders_for", AsyncMock()),
            patch.object(server, "sync_live_positions", AsyncMock()),
            patch.object(server, "_fetch_live_qty_cache", AsyncMock(return_value=dict(live_cache))),
            patch.object(server.binance_live, "cancel_all_algo_orders", AsyncMock(return_value={})) as cancel_algo,
            patch.object(
                server.binance_live,
                "place_market_order",
                AsyncMock(return_value={"orderId": 9001, "executedQty": "0.03"}),
            ) as place_order,
            patch.object(server.asyncio, "sleep", AsyncMock()) as sleep_mock,
        ):
            result = await server._emergency_close_batch(
                list(server.state["open_positions"].keys()),
                full_leg=True,
            )

        self.assertEqual(place_order.await_count, 2)
        self.assertEqual(cancel_algo.await_count, 2)
        self.assertEqual(sleep_mock.await_count, 1)
        self.assertEqual(result["closed"], 3)
        self.assertEqual(result["legs"], 2)
        self.assertEqual(server.state["open_positions"], {})
        btc_call = place_order.await_args_list[0]
        self.assertEqual(btc_call.args[1], "BTCUSDT")
        self.assertAlmostEqual(float(btc_call.args[3]), 0.03)

    async def test_emergency_close_strategy_caps_qty_when_sibling_on_leg(self):
        server.LIVE_MODE = True
        server._BINANCE_RATE_LIMIT_UNTIL_MS = 0
        server.state["open_positions"] = {
            "BTCUSDT_Tab1": {
                "tab": "Tab1",
                "symbol": "BTCUSDT",
                "side": "Long",
                "position_side": "LONG",
                "entry_price": 60000.0,
                "qty": 0.01,
                "entry_time": "2026-06-05T08:00:00",
            },
            "BTCUSDT_TabA": {
                "tab": "TabA",
                "symbol": "BTCUSDT",
                "side": "Long",
                "position_side": "LONG",
                "entry_price": 60100.0,
                "qty": 0.02,
                "entry_time": "2026-06-05T09:00:00",
            },
        }
        live_cache = {("BTCUSDT", "LONG"): 0.03}

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "_reconcile_pnl_from_binance", AsyncMock()),
            patch.object(server, "trigger_income_sync", AsyncMock()),
            patch.object(server, "_sweep_algo_orders_for", AsyncMock()),
            patch.object(server, "sync_live_positions", AsyncMock()),
            patch.object(server, "_fetch_live_qty_cache", AsyncMock(return_value=dict(live_cache))),
            patch.object(server.binance_live, "cancel_algo_order", AsyncMock(return_value={})),
            patch.object(
                server.binance_live,
                "place_market_order",
                AsyncMock(return_value={"orderId": 9002, "executedQty": "0.01"}),
            ) as place_order,
            patch.object(server.binance_live, "cancel_all_algo_orders", AsyncMock()) as cancel_all,
        ):
            result = await server._emergency_close_batch(["BTCUSDT_Tab1"], full_leg=False)

        cancel_all.assert_not_awaited()
        place_order.assert_awaited_once()
        self.assertAlmostEqual(float(place_order.await_args.args[3]), 0.01)
        self.assertEqual(result["closed"], 1)
        self.assertNotIn("BTCUSDT_Tab1", server.state["open_positions"])
        self.assertIn("BTCUSDT_TabA", server.state["open_positions"])

    async def test_emergency_close_batch_preflight_blocks_rate_limit(self):
        server.LIVE_MODE = True
        server._BINANCE_RATE_LIMIT_UNTIL_MS = int(
            (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp() * 1000
        )
        server.state["open_positions"] = {
            "ETHUSDT_Tab1": {
                "tab": "Tab1",
                "symbol": "ETHUSDT",
                "side": "Long",
                "position_side": "LONG",
                "qty": 0.01,
                "entry_time": "2026-06-05T08:00:00",
            }
        }
        with patch.object(server.binance_live, "place_market_order", AsyncMock()) as place_order:
            result = await server._emergency_close_batch(["ETHUSDT_Tab1"], full_leg=True)
        self.assertEqual(result.get("status"), "rate_limited")
        place_order.assert_not_awaited()
        self.assertIn("ETHUSDT_Tab1", server.state["open_positions"])
        server._BINANCE_RATE_LIMIT_UNTIL_MS = 0
        server._BINANCE_RATE_LIMIT_REASON = ""
        server._BINANCE_RATE_LIMIT_ALERT_SENT = False

    async def test_emergency_close_batch_stops_on_429_and_schedules_retry(self):
        server.LIVE_MODE = True
        server._BINANCE_RATE_LIMIT_UNTIL_MS = 0
        server._BINANCE_RATE_LIMIT_REASON = ""
        server._BINANCE_RATE_LIMIT_ALERT_SENT = False
        server._EMERGENCY_CLOSE_RETRY_TASK = None
        server._EMERGENCY_CLOSE_RETRY_PENDING = None
        server.state["open_positions"] = {
            "BTCUSDT_Tab1": {
                "tab": "Tab1",
                "symbol": "BTCUSDT",
                "side": "Long",
                "position_side": "LONG",
                "entry_price": 60000.0,
                "qty": 0.01,
                "entry_time": "2026-06-05T08:00:00",
            },
            "ETHUSDT_TabB": {
                "tab": "TabB",
                "symbol": "ETHUSDT",
                "side": "Short",
                "position_side": "SHORT",
                "entry_price": 3000.0,
                "qty": 0.05,
                "entry_time": "2026-06-05T08:30:00",
            },
        }
        live_cache = {
            ("BTCUSDT", "LONG"): 0.01,
            ("ETHUSDT", "SHORT"): 0.05,
        }
        rate_exc = httpx.HTTPStatusError(
            "429",
            request=httpx.Request("POST", "https://fapi.binance.com/fapi/v1/order"),
            response=httpx.Response(429, text='{"code":-1003,"msg":"Too many requests"}'),
        )

        async def place_side_effect(client, sym, side, qty, **kwargs):
            if sym == "ETHUSDT":
                raise rate_exc
            return {"orderId": 9001, "executedQty": str(qty)}

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "send_telegram", AsyncMock()),
            patch.object(server, "_reconcile_pnl_from_binance", AsyncMock()),
            patch.object(server, "trigger_income_sync", AsyncMock()),
            patch.object(server, "_sweep_algo_orders_for", AsyncMock()),
            patch.object(server, "sync_live_positions", AsyncMock()),
            patch.object(server, "_fetch_live_qty_cache", AsyncMock(return_value=dict(live_cache))),
            patch.object(server.binance_live, "cancel_all_algo_orders", AsyncMock(return_value={})),
            patch.object(
                server.binance_live,
                "place_market_order",
                AsyncMock(side_effect=place_side_effect),
            ) as place_order,
            patch.object(server.asyncio, "sleep", AsyncMock()),
            patch.object(server, "_schedule_emergency_close_retry") as schedule_retry,
        ):
            result = await server._emergency_close_batch(
                list(server.state["open_positions"].keys()),
                full_leg=True,
            )

        self.assertEqual(result.get("status"), "rate_limited")
        self.assertEqual(result["closed"], 1)
        self.assertNotIn("BTCUSDT_Tab1", server.state["open_positions"])
        self.assertIn("ETHUSDT_TabB", server.state["open_positions"])
        self.assertEqual(place_order.await_count, 2)
        schedule_retry.assert_called_once()
        retry_keys = schedule_retry.call_args[0][0]
        self.assertIn("ETHUSDT_TabB", retry_keys)
        server._EMERGENCY_CLOSE_RETRY_TASK = None
        server._EMERGENCY_CLOSE_RETRY_PENDING = None

    async def test_verify_live_position_protection_survives_rate_limit(self):
        server.LIVE_MODE = True
        server.state["open_positions"] = {
            "ETHUSDT_Tab1": {
                "tab": "Tab1",
                "symbol": "ETHUSDT",
                "side": "Long",
                "position_side": "LONG",
                "qty": 0.01,
                "protection_mode": "local",
            }
        }
        rate_exc = httpx.HTTPStatusError(
            "418",
            request=httpx.Request("GET", "https://fapi.binance.com/fapi/v2/positionRisk"),
            response=httpx.Response(418, text='{"code":-1003,"msg":"Way too many requests"}'),
        )

        with patch.object(server.binance_live, "get_position_risk", AsyncMock(side_effect=rate_exc)):
            ok, msg = await server._verify_live_position_protection("ETHUSDT_Tab1")

        self.assertFalse(ok)
        self.assertIn("could not verify live qty", msg)

    async def test_cleanup_failed_live_entry_removes_flat_state(self):
        server.LIVE_MODE = True
        server.state["open_positions"] = {
            "ETHUSDT_Tab1": {
                "tab": "Tab1",
                "symbol": "ETHUSDT",
                "side": "Long",
                "position_side": "LONG",
                "qty": 0.01,
                "entry_time": "2026-05-23T08:00:00",
                "protection_status": "entry_saved_pending_protection",
            }
        }

        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "_resolve_live_qty", AsyncMock(return_value=(0.0, None))),
            patch.object(server.binance_live, "place_market_order", AsyncMock()) as place_order,
        ):
            await server._cleanup_failed_live_entry(
                "ETHUSDT_Tab1", "ETHUSDT", "LONG", "SELL", 0.01, "Tab1"
            )

        self.assertNotIn("ETHUSDT_Tab1", server.state["open_positions"])
        place_order.assert_not_awaited()

    def test_binance_rate_limit_gate_blocks_entries(self):
        server._BINANCE_RATE_LIMIT_UNTIL_MS = int(
            (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp() * 1000
        )
        self.assertTrue(server._binance_rate_limited())
        server._BINANCE_RATE_LIMIT_UNTIL_MS = 0
        server._BINANCE_RATE_LIMIT_REASON = ""
        server._BINANCE_RATE_LIMIT_ALERT_SENT = False
        self.assertFalse(server._binance_rate_limited())

    def test_note_binance_rate_limit_parses_banned_until(self):
        server._BINANCE_RATE_LIMIT_UNTIL_MS = 0
        server._BINANCE_RATE_LIMIT_REASON = ""
        server._BINANCE_RATE_LIMIT_ALERT_SENT = False
        until_ms = int((datetime.now(timezone.utc) + timedelta(minutes=10)).timestamp() * 1000)
        server._note_binance_rate_limit(
            Exception(f'{{"code":-1003,"msg":"IP banned until {until_ms}"}}'),
        )
        self.assertEqual(server._BINANCE_RATE_LIMIT_UNTIL_MS, until_ms)
        snap = server._binance_rate_limit_snapshot()
        self.assertTrue(snap["active"])
        server._BINANCE_RATE_LIMIT_UNTIL_MS = 0
        server._BINANCE_RATE_LIMIT_REASON = ""
        server._BINANCE_RATE_LIMIT_ALERT_SENT = False

    def test_note_binance_rate_limit_default_backoff_without_until(self):
        server._BINANCE_RATE_LIMIT_UNTIL_MS = 0
        server._BINANCE_RATE_LIMIT_REASON = ""
        server._BINANCE_RATE_LIMIT_ALERT_SENT = False
        before = int(datetime.now(timezone.utc).timestamp() * 1000)
        server._note_binance_rate_limit(
            httpx.HTTPStatusError(
                "418",
                request=httpx.Request("GET", "https://fapi.binance.com/fapi/v1/order"),
                response=httpx.Response(418, text='{"code":-1003,"msg":"Way too many requests"}'),
            ),
        )
        self.assertGreater(server._BINANCE_RATE_LIMIT_UNTIL_MS, before)
        self.assertTrue(server._binance_rate_limited())
        server._BINANCE_RATE_LIMIT_UNTIL_MS = 0
        server._BINANCE_RATE_LIMIT_REASON = ""
        server._BINANCE_RATE_LIMIT_ALERT_SENT = False

    def test_health_snapshot_reports_api_ban(self):
        server._BINANCE_RATE_LIMIT_UNTIL_MS = int(
            (datetime.now(timezone.utc) + timedelta(minutes=2)).timestamp() * 1000
        )
        server._BINANCE_RATE_LIMIT_REASON = "test ban"
        snap = server._health_snapshot()
        self.assertTrue(snap["binance_rate_limit"]["active"])
        self.assertTrue(any("API ban" in r for r in snap["reasons"]))
        server._BINANCE_RATE_LIMIT_UNTIL_MS = 0
        server._BINANCE_RATE_LIMIT_REASON = ""
        server._BINANCE_RATE_LIMIT_ALERT_SENT = False

    def test_apply_uds_account_update_balance_and_position(self):
        server.exchange_account = {}
        server._apply_uds_account_update({
            "e": "ACCOUNT_UPDATE",
            "a": {
                "B": [{"a": "USDT", "wb": "1000.5", "cw": "900.25", "bc": "0"}],
                "P": [{
                    "s": "BTCUSDT",
                    "pa": "0.01",
                    "ep": "50000",
                    "up": "12.5",
                    "ps": "LONG",
                    "mt": "cross",
                    "iw": "0",
                }],
            },
        })
        ea = server.exchange_account
        self.assertAlmostEqual(ea["walletBalance"], 1000.5)
        self.assertAlmostEqual(ea["availableBalance"], 900.25)
        self.assertEqual(len(ea["positions"]), 1)
        self.assertEqual(ea["positions"][0]["symbol"], "BTCUSDT")
        self.assertEqual(ea["positions"][0]["side"], "Long")
        self.assertAlmostEqual(ea["positions"][0]["unrealizedProfit"], 12.5)
        self.assertEqual(ea["positions"][0]["leverage"], server.LEVERAGE)

    def test_apply_uds_account_update_preserves_leverage_from_rest(self):
        server._symbol_leverage["BTCUSDT"] = 5
        server.exchange_account = {
            "walletBalance": 1000.0,
            "availableBalance": 900.0,
            "positions": [{
                "symbol": "BTCUSDT",
                "side": "Long",
                "positionAmt": 0.01,
                "entryPrice": 50000.0,
                "breakEvenPrice": 50000.0,
                "markPrice": 50100.0,
                "unrealizedProfit": 10.0,
                "liquidationPrice": 40000.0,
                "leverage": 5,
                "marginType": "isolated",
                "isolatedWallet": 100.0,
                "isolatedMargin": 100.0,
                "notional": 500.0,
                "maintMargin": 2.0,
            }],
        }
        server._apply_uds_account_update({
            "e": "ACCOUNT_UPDATE",
            "a": {
                "P": [{
                    "s": "BTCUSDT",
                    "pa": "0.01",
                    "ep": "50000",
                    "up": "12.5",
                    "ps": "LONG",
                    "mt": "isolated",
                    "iw": "100",
                }],
            },
        })
        pos = server.exchange_account["positions"][0]
        self.assertEqual(pos["leverage"], 5)
        self.assertAlmostEqual(pos["markPrice"], 50100.0)
        self.assertAlmostEqual(pos["liquidationPrice"], 40000.0)
        self.assertAlmostEqual(pos["unrealizedProfit"], 12.5)

    def test_uds_account_fresh_requires_recent_update(self):
        import time as time_mod
        server._uds_connected = True
        server._last_uds_account_update_mono = time_mod.monotonic()
        self.assertTrue(server._uds_account_fresh())
        server._last_uds_account_update_mono = time_mod.monotonic() - (server.UDS_ACCOUNT_FRESH_SEC + 60)
        self.assertFalse(server._uds_account_fresh())

    def test_entry_window_blocks_and_releases(self):
        import time as time_mod
        server._entry_busy_until_mono = 0.0
        self.assertFalse(server._entry_window_active())
        server._begin_entry_window()
        self.assertTrue(server._entry_window_active())
        server._release_entry_busy_after_eval()
        self.assertTrue(server._entry_window_active())
        server._entry_busy_until_mono = time_mod.monotonic() - 1.0
        self.assertFalse(server._entry_window_active())

    async def test_fetch_close_pnl_skips_during_entry_window(self):
        server._entry_busy_until_mono = 0.0
        server._begin_entry_window()
        result = await server._fetch_close_pnl_from_trades(
            "BTCUSDT", "LONG", "Long", 0.01, "2026-01-01T00:00:00+00:00",
        )
        self.assertIsNone(result)

    def test_next_trade_history_batch_rotates(self):
        server._BINANCE_HISTORY_SYMBOL_INDEX = 0
        server.SCAN_SYMBOLS = [f"SYM{i}USDT" for i in range(20)]
        server.state["open_positions"] = {}
        server.state["history"] = []
        server.exchange_account = {"positions": []}
        batch1 = server._next_trade_history_batch(force=False)
        batch2 = server._next_trade_history_batch(force=False)
        self.assertEqual(len(batch1), server.BINANCE_CLOSE_HISTORY_BATCH_SIZE)
        self.assertEqual(len(batch2), server.BINANCE_CLOSE_HISTORY_BATCH_SIZE)
        self.assertNotEqual(batch1, batch2)

    def test_interval_has_enabled_tabs_respects_timeframe(self):
        server.state["tab_enabled"] = {tab: False for tab in server.TABS}
        server.state["tab_enabled"]["Tab7"] = True
        self.assertTrue(server._interval_has_enabled_tabs("4h"))
        self.assertFalse(server._interval_has_enabled_tabs("1h"))
        server.state["tab_enabled"]["Tab11"] = True
        self.assertTrue(server._interval_has_enabled_tabs("1h"))

    def test_symbols_for_interval_scan_uses_per_tab_override(self):
        server.SCAN_SYMBOLS = [f"SYM{i}USDT" for i in range(500)]
        server.state["symbol_scan_limit"] = 100
        server.state["symbol_scan_limit_by_tab"] = {"Tab17": 500}
        server.state["tab_enabled"] = {tab: tab == "Tab17" for tab in server.TABS}
        syms = server._symbols_for_interval_scan("1h")
        self.assertEqual(len(syms), 500)
        self.assertEqual(syms[0], "SYM0USDT")
        self.assertEqual(syms[-1], "SYM499USDT")

    def test_symbols_for_interval_scan_empty_when_no_enabled_tabs(self):
        server.SCAN_SYMBOLS = [f"SYM{i}USDT" for i in range(100)]
        server.state["tab_enabled"] = {tab: False for tab in server.TABS}
        self.assertEqual(server._symbols_for_interval_scan("4h"), [])

    def test_prescreen_builds_short_only_watchlist(self):
        import config
        from bot.engine.prescreen import build_prescreen_watchlist

        with patch.object(config, "KLINE_PRESCREEN_ENABLED", True):
            server.SCAN_SYMBOLS = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
            server.state["trade_side_mode"] = "short_only"
            server.state["tab_enabled"] = {tab: tab == "Tab11" for tab in server.TABS}
            server.SCAN_TICKER_BY_SYM = {
                "AAAUSDT": {
                    "priceChangePercent": "-3.0",
                    "lastPrice": "92",
                    "lowPrice": "90",
                    "highPrice": "110",
                    "quoteVolume": "5000000",
                },
                "BBBUSDT": {
                    "priceChangePercent": "-4.0",
                    "lastPrice": "91",
                    "lowPrice": "90",
                    "highPrice": "110",
                    "quoteVolume": "5000000",
                },
                "CCCUSDT": {
                    "priceChangePercent": "-3.0",
                    "lastPrice": "108",
                    "lowPrice": "90",
                    "highPrice": "110",
                    "quoteVolume": "5000000",
                },
            }
            watchlist = build_prescreen_watchlist("1h")
            self.assertIn("BBBUSDT", watchlist)
            self.assertIn("AAAUSDT", watchlist)
            self.assertNotIn("CCCUSDT", watchlist)

    def test_symbols_for_interval_scan_uses_prescreen_watchlist(self):
        import config
        from datetime import datetime, timezone

        with patch.object(config, "KLINE_PRESCREEN_ENABLED", True):
            server.SCAN_SYMBOLS = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
            server.state["tab_enabled"] = {tab: tab == "Tab11" for tab in server.TABS}
            server.state["prescreen_watchlists"] = {
                "1h": {"slot": "2026-06-20T15:00", "symbols": ["BBBUSDT", "AAAUSDT"]},
            }
            slot = datetime(2026, 6, 20, 15, 0, tzinfo=timezone.utc)
            syms = server._symbols_for_interval_scan("1h", slot)
            self.assertEqual(syms, ["BBBUSDT", "AAAUSDT"])

    def test_scan_universe_size_includes_per_tab_override(self):
        server.state["symbol_scan_limit"] = 100
        server.state["symbol_scan_limit_by_tab"] = {"Tab17": 500}
        self.assertEqual(server._scan_universe_size(), 500)

    def test_clamp_symbol_scan_limit_tab17_accepts_500(self):
        self.assertEqual(server._clamp_symbol_scan_limit(500, "Tab17"), 500)
        self.assertEqual(server._clamp_symbol_scan_limit(500), 500)
        self.assertEqual(server._clamp_symbol_scan_limit(250), 200)

    def test_tab17_momentum_universe_filters_and_ranks(self):
        server.SCAN_SYMBOLS = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
        server.SCAN_TICKER_BY_SYM = {
            "AAAUSDT": {"priceChangePercent": "3.0"},
            "BBBUSDT": {"priceChangePercent": "-4.0"},
            "CCCUSDT": {"priceChangePercent": "1.0"},
        }

        def _ohlcv(vol_closed, vol_hist):
            rows = []
            for i in range(25):
                vol = vol_closed if i == 23 else vol_hist
                rows.append([i, 1, 1, 1, 1, vol])
            return rows

        symbols = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
        results = [
            _ohlcv(200.0, 100.0),
            _ohlcv(300.0, 100.0),
            _ohlcv(500.0, 100.0),
        ]
        universe = server._build_tab17_momentum_universe(symbols, results)
        self.assertIn("BBBUSDT", universe)
        self.assertIn("AAAUSDT", universe)
        self.assertNotIn("CCCUSDT", universe)
        self.assertGreater(universe["BBBUSDT"], universe["AAAUSDT"])

    def test_tab17_max_positions_uses_config_cap(self):
        server.state["max_positions_per_tab"] = 20
        self.assertEqual(server._tab_max_positions("Tab17"), 40)
        self.assertEqual(server._tab_max_positions("Tab11"), 20)

    def test_kline_request_weight_bins(self):
        self.assertEqual(server._kline_request_weight(100), 1)
        self.assertEqual(server._kline_request_weight(250), 5)
        self.assertEqual(server._kline_request_weight(400), 5)

    def test_interval_candle_closed_at_1h_every_hour(self):
        from datetime import datetime, timezone
        for hour in range(24):
            slot = datetime(2026, 6, 11, hour, 0, tzinfo=timezone.utc)
            self.assertTrue(server._interval_candle_closed_at(slot, "1h"))

    def test_interval_candle_closed_at_4h_on_boundary_only(self):
        from datetime import datetime, timezone
        for hour in range(24):
            slot = datetime(2026, 6, 11, hour, 0, tzinfo=timezone.utc)
            closed = server._interval_candle_closed_at(slot, "4h")
            self.assertEqual(closed, hour % 4 == 0)

    async def test_check_invalidations_skips_4h_positions_off_boundary(self):
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock, patch

        server.state["open_positions"] = {
            "BTCUSDT_Tab7": {"symbol": "BTCUSDT", "tab": "Tab7", "side": "Long"},
        }
        hour_slot = datetime(2026, 6, 11, 5, 0, tzinfo=timezone.utc)
        with patch.object(server, "get_klines", AsyncMock(return_value=[])) as mock_klines:
            await server.check_invalidations_loop(hour_slot=hour_slot)
        mock_klines.assert_not_called()

    async def test_check_invalidations_checks_4h_positions_on_boundary(self):
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock, patch

        server.state["open_positions"] = {
            "BTCUSDT_Tab7": {"symbol": "BTCUSDT", "tab": "Tab7", "side": "Long"},
        }
        hour_slot = datetime(2026, 6, 11, 4, 0, tzinfo=timezone.utc)
        with patch.object(server, "get_klines", AsyncMock(return_value=[])) as mock_klines:
            await server.check_invalidations_loop(hour_slot=hour_slot)
        mock_klines.assert_called_once()

    def test_symbols_for_trade_history_prioritizes_open_positions(self):
        server.SCAN_SYMBOLS = [f"SYM{i}USDT" for i in range(100)]
        server.state["open_positions"] = {
            "BTCUSDT_Tab1": {"symbol": "BTCUSDT", "tab": "Tab1"},
        }
        syms = server._symbols_for_trade_history(full_cap=False)
        self.assertIn("BTCUSDT", syms)
        self.assertLessEqual(len(syms), server.BINANCE_CLOSE_HISTORY_REFRESH_SYMBOL_CAP)

    def test_user_data_stream_ws_url_uses_path_listen_key(self):
        import binance_live
        url = binance_live.user_data_stream_ws_url("abc123KEY")
        self.assertIn("/private/ws/abc123KEY", url)
        self.assertNotIn("?listenKey=", url)
        self.assertNotIn("events=", url)


class PriceWsMessageTests(unittest.TestCase):
    def setUp(self):
        self.orig_top = list(server.SCAN_SYMBOLS)
        self.orig_prices = dict(server.latest_prices)
        self.orig_marks = dict(server.latest_marks)
        server.SCAN_SYMBOLS = ["BTCUSDT", "ETHUSDT"]
        server.latest_prices.clear()
        server.latest_marks.clear()

    def tearDown(self):
        server.SCAN_SYMBOLS[:] = self.orig_top
        server.latest_prices.clear()
        server.latest_prices.update(self.orig_prices)
        server.latest_marks.clear()
        server.latest_marks.update(self.orig_marks)

    def test_mini_ticker_combined_stream_wrapper(self):
        server._process_binance_price_ws_message({
            "stream": "!miniTicker@arr",
            "data": [{
                "e": "24hrMiniTicker",
                "E": 123,
                "s": "BTCUSDT",
                "c": "65000.10",
            }],
        })
        self.assertAlmostEqual(server.latest_prices["BTCUSDT"], 65000.10)

    def test_mark_price_combined_stream_wrapper(self):
        server._process_binance_price_ws_message({
            "stream": "!markPrice@arr@1s",
            "data": [{
                "e": "markPriceUpdate",
                "E": 123,
                "s": "BTCUSDT",
                "p": "64999.50",
            }],
        })
        self.assertAlmostEqual(server.latest_marks["BTCUSDT"], 64999.50)

    def test_is_ws_last_price_row_accepts_mini_and_legacy_ticker(self):
        self.assertTrue(server._is_ws_last_price_row({
            "e": "24hrMiniTicker", "s": "BTCUSDT", "c": "1",
        }))
        self.assertTrue(server._is_ws_last_price_row({
            "e": "24hrTicker", "s": "BTCUSDT", "c": "1",
        }))
        self.assertFalse(server._is_ws_last_price_row({
            "e": "markPriceUpdate", "s": "BTCUSDT", "p": "1",
        }))


class Entry4192RetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.orig_state = copy.deepcopy(server.state)
        self.orig_live_mode = server.LIVE_MODE
        self.orig_pending = list(server._pending_entry_retries)
        server.LIVE_MODE = True
        server._pending_entry_retries.clear()
        server.state = {
            "balances": {"Tab11": 10_000.0},
            "unrealized_pnls": {},
            "open_positions": {},
            "position_registry": {},
            "history": [],
            "used_setups": [],
            "sync_issues": [],
            "error_events": [],
        }
        server.latest_prices["BTCUSDT"] = 100.0

    def tearDown(self):
        server.state = self.orig_state
        server.LIVE_MODE = self.orig_live_mode
        server._pending_entry_retries[:] = self.orig_pending

    def test_is_binance_cooling_off_error(self):
        err = httpx.HTTPStatusError(
            "cooling off",
            request=httpx.Request("POST", "https://fapi.binance.com/fapi/v1/order"),
            response=httpx.Response(400, text='{"code":-4192,"msg":"Cooling-off Period"}'),
        )
        self.assertTrue(server._is_binance_cooling_off_error(err))
        self.assertFalse(server._is_binance_cooling_off_error(None, '{"code":-2019}'))

    async def test_queue_entry_retries_dedupes(self):
        item = {
            "sym": "BTCUSDT",
            "setup_key": "k1",
            "sig": {"ep": 100.0, "side": "Long", "sl": 90.0, "tp": 110.0},
        }
        await server._queue_entry_retries("Tab11", [item, item])
        self.assertEqual(len(server._pending_entry_retries), 1)
        self.assertEqual(server._pending_entry_retries[0]["attempt"], 1)

    async def test_open_balanced_halts_on_cooling_off(self):
        candidates = [
            {"sym": "AAAUSDT", "setup_key": "a", "sig": {"ep": 1.0, "side": "Long", "sl": 0.9, "tp": 1.1}},
            {"sym": "BBBUSDT", "setup_key": "b", "sig": {"ep": 2.0, "side": "Short", "sl": 2.1, "tp": 1.9}},
            {"sym": "CCCUSDT", "setup_key": "c", "sig": {"ep": 3.0, "side": "Long", "sl": 2.9, "tp": 3.1}},
        ]
        tab_counts = {"Tab11": 0}
        calls = []

        async def fake_execute(sym, sig, tab_name, **kwargs):
            calls.append(sym)
            if sym == "BBBUSDT":
                return "cooling_off"
            server.state["open_positions"][f"{sym}_{tab_name}"] = {"tab": tab_name, "symbol": sym}
            return None

        with patch.object(server, "ENTRY_WAIT_FOR_BETTER_PRICE", False):
            with patch.object(server, "execute_entry", side_effect=fake_execute):
                await server._open_balanced_candidates("Tab11", candidates, tab_counts)

        self.assertEqual(calls, ["AAAUSDT", "BBBUSDT"])
        self.assertEqual(len(server._pending_entry_retries), 2)
        queued_syms = {r["sym"] for r in server._pending_entry_retries}
        self.assertEqual(queued_syms, {"BBBUSDT", "CCCUSDT"})

    def test_entry_price_at_or_better(self):
        with patch.object(server, "ENTRY_MIN_PRICE_IMPROVE_PCT", 0):
            self.assertTrue(server._entry_price_at_or_better("Long", 99.0, 100.0))
            self.assertTrue(server._entry_price_at_or_better("Long", 100.0, 100.0))
            self.assertFalse(server._entry_price_at_or_better("Long", 101.0, 100.0))
            self.assertTrue(server._entry_price_at_or_better("Short", 101.0, 100.0))
            self.assertTrue(server._entry_price_at_or_better("Short", 100.0, 100.0))
            self.assertFalse(server._entry_price_at_or_better("Short", 99.0, 100.0))

    def test_entry_price_min_improve_pct(self):
        with patch.object(server, "ENTRY_MIN_PRICE_IMPROVE_PCT", 0.001):
            # Long ep=100: need mark <= 99.9 (0.1% below)
            self.assertTrue(server._entry_price_at_or_better("Long", 99.9, 100.0))
            self.assertFalse(server._entry_price_at_or_better("Long", 99.91, 100.0))
            self.assertFalse(server._entry_price_at_or_better("Long", 100.0, 100.0))
            # Short ep=100: need mark >= 100.1
            self.assertTrue(server._entry_price_at_or_better("Short", 100.1, 100.0))
            self.assertFalse(server._entry_price_at_or_better("Short", 100.09, 100.0))
            self.assertFalse(server._entry_price_at_or_better("Short", 100.0, 100.0))

    async def test_retry_waits_until_price_favorable(self):
        server.latest_prices["BTCUSDT"] = 101.0
        retry_item = {
            "sym": "BTCUSDT",
            "tab_name": "Tab11",
            "setup_key": "k1",
            "signal_ep": 100.0,
            "attempt": 1,
            "sig": {"ep": 100.0, "side": "Long", "sl": 90.0, "tp": 110.0},
        }
        poll_calls = {"n": 0}

        async def fake_sleep(sec):
            poll_calls["n"] += 1
            if poll_calls["n"] == 1:
                server.latest_prices["BTCUSDT"] = 99.5

        async def fake_execute(sym, sig, tab_name, **kwargs):
            server.state["open_positions"][f"{sym}_{tab_name}"] = {"tab": tab_name, "symbol": sym}
            return None

        with patch.object(server.asyncio, "sleep", side_effect=fake_sleep):
            with patch.object(server, "execute_entry", side_effect=fake_execute):
                await server._retry_one_queued_entry(retry_item, block_until_price=True)
        self.assertGreaterEqual(poll_calls["n"], 1)
        self.assertIn("BTCUSDT_Tab11", server.state["open_positions"])

    async def test_retry_executes_when_price_favorable(self):
        server.latest_prices["BTCUSDT"] = 99.9
        retry_item = {
            "sym": "BTCUSDT",
            "tab_name": "Tab11",
            "setup_key": "k1",
            "signal_ep": 100.0,
            "attempt": 1,
            "sig": {"ep": 100.0, "side": "Long", "sl": 90.0, "tp": 110.0},
        }

        async def fake_execute(sym, sig, tab_name, **kwargs):
            server.state["open_positions"][f"{sym}_{tab_name}"] = {"tab": tab_name, "symbol": sym}
            return None

        with patch.object(server, "execute_entry", side_effect=fake_execute):
            await server._retry_one_queued_entry(retry_item)
        self.assertIn("BTCUSDT_Tab11", server.state["open_positions"])
        self.assertIn("k1", server.state["used_setups"])

    def test_entry_retry_signal_expired_after_10_min(self):
        import time
        now_ms = int(time.time() * 1000)
        close_ms = now_ms - (11 * 60 * 1000)
        signal_ts_ms = close_ms - (3600 * 1000)
        expired, age = server._entry_retry_signal_expired("Tab11", signal_ts_ms)
        self.assertTrue(expired)
        self.assertGreater(age, server.ENTRY_4192_RETRY_MAX_AGE_SEC)

    def test_entry_retry_signal_fresh_within_10_min(self):
        import time
        now_ms = int(time.time() * 1000)
        close_ms = now_ms - (5 * 60 * 1000)
        signal_ts_ms = close_ms - (3600 * 1000)
        expired, _age = server._entry_retry_signal_expired("Tab11", signal_ts_ms)
        self.assertFalse(expired)

    async def test_retry_does_not_enter_while_price_unfavorable(self):
        server.latest_prices["BTCUSDT"] = 101.0
        retry_item = {
            "sym": "BTCUSDT",
            "tab_name": "Tab11",
            "setup_key": "k1",
            "signal_ep": 100.0,
            "attempt": 1,
            "sig": {"ep": 100.0, "side": "Long", "sl": 90.0, "tp": 110.0},
        }

        async def stop_after_one_sleep(sec):
            raise asyncio.CancelledError()

        with patch.object(server.asyncio, "sleep", side_effect=stop_after_one_sleep):
            with patch.object(server, "execute_entry", new_callable=AsyncMock) as mock_entry:
                with self.assertRaises(asyncio.CancelledError):
                    await server._retry_one_queued_entry(retry_item, block_until_price=True)
        mock_entry.assert_not_called()

    async def test_process_due_retries_ready_first(self):
        import time
        now = time.monotonic()
        now_ms = int(time.time() * 1000)
        close_ms = now_ms - (2 * 60 * 1000)
        signal_ts_ms = close_ms - (3600 * 1000)
        server.latest_prices["AAAUSDT"] = 101.0
        server.latest_prices["BBBUSDT"] = 99.0
        server._pending_entry_retries[:] = [
            {
                "tab_name": "Tab11",
                "sym": "AAAUSDT",
                "setup_key": "a",
                "signal_ep": 100.0,
                "signal_ts_ms": signal_ts_ms,
                "retry_at_mono": now - 1,
                "attempt": 1,
                "sig": {"ep": 100.0, "side": "Long", "sl": 90.0, "tp": 110.0},
            },
            {
                "tab_name": "Tab11",
                "sym": "BBBUSDT",
                "setup_key": "b",
                "signal_ep": 100.0,
                "signal_ts_ms": signal_ts_ms,
                "retry_at_mono": now - 1,
                "attempt": 1,
                "sig": {"ep": 100.0, "side": "Long", "sl": 90.0, "tp": 110.0},
            },
        ]
        entered = []

        async def fake_execute(sym, sig, tab_name, **kwargs):
            entered.append(sym)
            server.state["open_positions"][f"{sym}_{tab_name}"] = {"tab": tab_name, "symbol": sym}
            return None

        with patch.object(server, "execute_entry", side_effect=fake_execute):
            with patch.object(server, "_stagger_before_next_entry", new_callable=AsyncMock):
                await server._process_due_entry_retries()

        self.assertEqual(entered, ["BBBUSDT"])
        deferred = [r for r in server._pending_entry_retries if r["sym"] == "AAAUSDT"]
        self.assertEqual(len(deferred), 1)

    def test_entry_price_favorability_pct(self):
        self.assertGreater(
            server._entry_price_favorability_pct("Long", 99.0, 100.0),
            server._entry_price_favorability_pct("Long", 99.5, 100.0),
        )

    async def test_retry_skips_when_signal_expired(self):
        import time
        server.latest_prices["BTCUSDT"] = 100.05
        now_ms = int(time.time() * 1000)
        close_ms = now_ms - (11 * 60 * 1000)
        retry_item = {
            "sym": "BTCUSDT",
            "tab_name": "Tab11",
            "setup_key": "BTCUSDT_Tab11_1",
            "signal_ts_ms": close_ms - (3600 * 1000),
            "signal_ep": 100.0,
            "attempt": 1,
            "sig": {"ep": 100.0, "side": "Long", "sl": 90.0, "tp": 110.0},
        }
        with patch.object(server, "execute_entry", new_callable=AsyncMock) as mock_entry:
            await server._retry_one_queued_entry(retry_item)
        mock_entry.assert_not_called()


class LongShortBalanceEntryTests(unittest.TestCase):
    def setUp(self):
        self.orig_state = copy.deepcopy(server.state)
        server.state = {
            "open_positions": {},
            "long_short_balance_mode": "cap",
            "max_positions_per_tab": 40,
        }

    def tearDown(self):
        server.state = self.orig_state

    def _seed_shorts(self, tab: str, n: int) -> None:
        for i in range(n):
            server.state["open_positions"][f"S{i}_{tab}"] = {
                "tab": tab,
                "side": "Short",
                "symbol": f"S{i}USDT",
            }

    def test_cap_allows_long_when_short_at_side_cap(self):
        self._seed_shorts("Tab11", 20)
        ok, reason = server._entry_long_short_balance_allowed("Tab11", "Long")
        self.assertTrue(ok, reason)
        ok, reason = server._entry_long_short_balance_allowed("Tab11", "Short")
        self.assertFalse(ok)
        self.assertIn("50/50 cap", reason)

    def test_cap_off_always_allows(self):
        server.state["long_short_balance_mode"] = "off"
        self._seed_shorts("Tab11", 25)
        ok, reason = server._entry_long_short_balance_allowed("Tab11", "Short")
        self.assertTrue(ok, reason)

    def test_nearly_blocks_imbalanced_side(self):
        server.state["long_short_balance_mode"] = "nearly"
        self._seed_shorts("Tab11", 10)
        ok, reason = server._entry_long_short_balance_allowed("Tab11", "Short")
        self.assertFalse(ok)
        self.assertIn("nearly balance", reason)


class ExecuteEntryBalanceGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.orig_state = copy.deepcopy(server.state)
        self.orig_live_mode = server.LIVE_MODE
        server.LIVE_MODE = False
        server._circuit_breaker = False
        server.state = {
            "balances": {"Tab11": 10_000.0},
            "open_positions": {},
            "used_setups": [],
            "long_short_balance_mode": "cap",
            "max_positions_per_tab": 40,
        }
        for i in range(20):
            server.state["open_positions"][f"S{i}_Tab11"] = {
                "tab": "Tab11",
                "side": "Short",
                "symbol": f"S{i}USDT",
            }

    def tearDown(self):
        server.state = self.orig_state
        server.LIVE_MODE = self.orig_live_mode

    async def test_execute_entry_skips_when_cap_side_full(self):
        sig = {"side": "Short", "ep": 100.0, "sl": 96.0, "tp": 108.0}
        with patch.object(server, "_fetch_entry_reference_price", AsyncMock(return_value=100.0)):
            await server._execute_entry_unsafe("ETHUSDT", sig, "Tab11")
        self.assertNotIn("ETHUSDT_Tab11", server.state["open_positions"])


class EntryPriceWaitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.orig_state = copy.deepcopy(server.state)
        self.orig_live_mode = server.LIVE_MODE
        self.orig_wait = server.ENTRY_WAIT_FOR_BETTER_PRICE
        self.orig_pending = list(server._pending_entry_retries)
        server.LIVE_MODE = False
        server.ENTRY_WAIT_FOR_BETTER_PRICE = True
        server._pending_entry_retries.clear()
        server.state = {
            "balances": {"Tab11": 10_000.0},
            "unrealized_pnls": {},
            "open_positions": {},
            "position_registry": {},
            "history": [],
            "used_setups": [],
            "sync_issues": [],
            "error_events": [],
        }
        server.latest_prices["BTCUSDT"] = 101.0

    def tearDown(self):
        server.state = self.orig_state
        server.LIVE_MODE = self.orig_live_mode
        server.ENTRY_WAIT_FOR_BETTER_PRICE = self.orig_wait
        server._pending_entry_retries[:] = self.orig_pending

    async def test_defer_when_mark_worse_than_ep(self):
        import time

        signal_ts_ms = int((time.time() - 60) * 1000)
        setup_key = f"BTCUSDT_Tab11_{signal_ts_ms}"
        sig = {"side": "Long", "ep": 100.0, "sl": 90.0, "tp": 110.0}
        with patch.object(server, "_fetch_entry_reference_price", AsyncMock(return_value=101.0)):
            with patch.object(server, "save_state", AsyncMock()):
                result = await server._execute_entry_unsafe(
                    "BTCUSDT", sig, "Tab11", setup_key=setup_key,
                )
        self.assertEqual(result, "price_wait")
        self.assertEqual(len(server._pending_entry_retries), 1)
        self.assertEqual(server._pending_entry_retries[0]["queue_kind"], "price_wait")

    async def test_no_defer_when_mark_at_or_better(self):
        server.latest_prices["BTCUSDT"] = 99.5
        sig = {"side": "Long", "ep": 100.0, "sl": 90.0, "tp": 110.0}
        with patch.object(server, "_fetch_entry_reference_price", AsyncMock(return_value=99.5)):
            result = await server._execute_entry_unsafe(
                "BTCUSDT", sig, "Tab11", setup_key="BTCUSDT_Tab11_1",
            )
        self.assertNotEqual(result, "price_wait")
        self.assertEqual(len(server._pending_entry_retries), 0)

    async def test_skip_price_wait_bypasses_defer(self):
        sig = {"side": "Long", "ep": 100.0, "sl": 90.0, "tp": 110.0}
        with patch.object(server, "_fetch_entry_reference_price", AsyncMock(return_value=105.0)):
            with patch.object(server, "_paper_simulate_entry_fill", AsyncMock(return_value=105.0)):
                with patch.object(server, "save_state", AsyncMock()):
                    result = await server._execute_entry_unsafe(
                        "BTCUSDT",
                        sig,
                        "Tab11",
                        setup_key="BTCUSDT_Tab11_1",
                        skip_price_wait=True,
                    )
        self.assertNotEqual(result, "price_wait")
        self.assertEqual(len(server._pending_entry_retries), 0)

    async def test_price_wait_timeout_marks_setup_used(self):
        import time

        old_max = server.ENTRY_PRICE_WAIT_MAX_SEC
        server.ENTRY_PRICE_WAIT_MAX_SEC = 1
        try:
            signal_ts_ms = int((time.time() - 3602) * 1000)  # 1h candle closed ~2s ago
            setup_key = f"BTCUSDT_Tab11_{signal_ts_ms}"
            retry_item = {
                "sym": "BTCUSDT",
                "tab_name": "Tab11",
                "setup_key": setup_key,
                "signal_ep": 100.0,
                "signal_ts_ms": signal_ts_ms,
                "attempt": 0,
                "queue_kind": "price_wait",
                "sig": {"ep": 100.0, "side": "Long", "sl": 90.0, "tp": 110.0},
            }
            server.latest_prices["BTCUSDT"] = 101.0
            ready, waiting = server._partition_due_entry_retries([retry_item])
            self.assertEqual(ready, [])
            self.assertEqual(waiting, [])
            self.assertIn(setup_key, server.state["used_setups"])
        finally:
            server.ENTRY_PRICE_WAIT_MAX_SEC = old_max

    async def test_process_queue_enters_when_price_turns_favorable(self):
        import time

        signal_ts_ms = int(time.time() * 1000)
        setup_key = f"BTCUSDT_Tab11_{signal_ts_ms}"
        server._pending_entry_retries.append({
            "tab_name": "Tab11",
            "sym": "BTCUSDT",
            "setup_key": setup_key,
            "signal_ep": 100.0,
            "signal_ts_ms": signal_ts_ms,
            "retry_at_mono": time.monotonic() - 1,
            "attempt": 0,
            "queue_kind": "price_wait",
            "sig": {"ep": 100.0, "side": "Long", "sl": 90.0, "tp": 110.0},
        })
        server.latest_prices["BTCUSDT"] = 99.0

        async def fake_execute(sym, sig, tab_name, **kwargs):
            server.state["open_positions"][f"{sym}_{tab_name}"] = {"tab": tab_name, "symbol": sym}
            return None

        with patch.object(server, "execute_entry", side_effect=fake_execute):
            with patch.object(server, "save_state", AsyncMock()):
                await server._process_due_entry_retries()
        self.assertIn("BTCUSDT_Tab11", server.state["open_positions"])
        self.assertEqual(len(server._pending_entry_retries), 0)


class EntryGuardTests(unittest.TestCase):
    def test_mark_within_entry_protection_long(self):
        ok, _ = server._mark_within_entry_protection("BTCUSDT", "Long", 100.0, 93.0, 110.0)
        self.assertTrue(ok)
        bad, reason = server._mark_within_entry_protection("BTCUSDT", "Long", 100.0, 101.0, 110.0)
        self.assertFalse(bad)
        self.assertIn("outside", reason)

    def test_mark_within_entry_protection_short(self):
        ok, _ = server._mark_within_entry_protection("BTCUSDT", "Short", 100.0, 107.0, 93.0)
        self.assertTrue(ok)
        bad, _ = server._mark_within_entry_protection("BTCUSDT", "Short", 100.0, 99.0, 93.0)
        self.assertFalse(bad)

    def test_effective_kline_delay_at_least_min(self):
        with patch.object(server, "KLINE_FETCH_DELAY_SEC", 3):
            with patch.object(server, "KLINE_FETCH_MIN_DELAY_SEC", 10):
                self.assertEqual(server._effective_kline_fetch_delay_sec(), 10)

    def test_local_monitor_grace_blocks_exit_checks(self):
        import time

        pos = {
            "protection_mode": "local",
            "sl_source": "local",
            "local_monitor_after_mono": time.monotonic() + 60,
        }
        self.assertTrue(server._position_in_local_monitor_grace(pos))
        pos["local_monitor_after_mono"] = time.monotonic() - 1
        self.assertFalse(server._position_in_local_monitor_grace(pos))


class EntryGuardAsyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.orig_state = copy.deepcopy(server.state)
        self.orig_live = server.LIVE_MODE
        server.LIVE_MODE = True
        server.state = {
            "balances": {"Tab11": 10_000.0},
            "open_positions": {},
            "position_registry": {},
            "history": [],
            "used_setups": [],
            "sync_issues": [],
            "error_events": [],
        }

    def tearDown(self):
        server.state = self.orig_state
        server.LIVE_MODE = self.orig_live

    async def test_pre_entry_guard_skips_when_sl_above_mark(self):
        sig = {"side": "Long", "ep": 1.78, "sl": 1.6559, "tp": 1.93634}
        with patch.object(server, "_fetch_sltp_trigger_price", AsyncMock(return_value=1.55724)):
            ok, reason = await server._pre_entry_mark_protection_guard("VELVETUSDT", sig)
        self.assertFalse(ok)
        self.assertIn("outside", reason)

    async def test_resolve_live_entry_fill_from_get_order(self):
        server._http_client = object()
        entry_res = {"orderId": 99, "avgPrice": "0", "executedQty": "0"}
        with patch.object(
            server.binance_live,
            "get_order",
            AsyncMock(return_value={"avgPrice": "1.55218", "executedQty": "6"}),
        ):
            with patch.object(server.asyncio, "sleep", AsyncMock()):
                px, qty, source = await server._resolve_live_entry_fill(
                    "VELVETUSDT", entry_res, entry_client_id="cid",
                )
        self.assertAlmostEqual(px, 1.55218)
        self.assertAlmostEqual(qty, 6.0)
        self.assertEqual(source, "get_order")


class SymbolFilterTests(unittest.TestCase):
    @staticmethod
    def _history_closes(tab, sym, wins, losses, win_pnl=1.0, loss_pnl=-1.0, side="Long"):
        rows = []
        for _ in range(wins):
            rows.append({"tab": tab, "symbol": sym, "side": side, "pnl_usd": win_pnl})
        for _ in range(losses):
            rows.append({"tab": tab, "symbol": sym, "side": side, "pnl_usd": loss_pnl})
        return rows

    def setUp(self):
        self.orig_state = copy.deepcopy(server.state)
        beat_hist = self._history_closes("Tab11", "BEATUSDT", 8, 2, loss_pnl=-1.5)
        ubu_hist = self._history_closes("Tab11", "UBUSDT", 1, 7, loss_pnl=-5.0 / 7, side="Short")
        server.state = {
            "balances": {"Tab11": 10_000.0},
            "unrealized_pnls": {},
            "open_positions": {},
            "position_registry": {},
            "history": beat_hist + ubu_hist,
            "used_setups": [],
            "sync_issues": [],
            "error_events": [],
            "symbol_stats": {
                "Tab11": {
                    "BEATUSDT": {
                        "trades": 10, "wins": 8, "net_pnl": 5.0,
                        "long_trades": 10, "short_trades": 0,
                        "long_pnl": 5.0, "short_pnl": 0.0,
                    },
                    "UBUSDT": {
                        "trades": 8, "wins": 1, "net_pnl": -4.0,
                        "long_trades": 2, "short_trades": 6,
                        "long_pnl": -1.0, "short_pnl": -3.0,
                    },
                },
            },
            "symbol_stats_version": server._SYMBOL_STATS_VERSION,
            "symbol_filter_by_tab": {
                "Tab11": {
                    "mode": "auto_winners",
                    "min_trades": 5,
                    "min_win_rate": 0.60,
                    "min_net_pnl": 0.50,
                },
            },
            "symbol_allowlist_by_tab": {"Tab11": []},
            "symbol_blocklist_by_tab": {"Tab11": ["UBUSDT"]},
        }
        server._invalidate_rolling_symbol_stats_cache()

    def tearDown(self):
        server.state = self.orig_state
        server._invalidate_rolling_symbol_stats_cache()

    def test_auto_winner_symbols(self):
        winners = server._auto_winner_symbols("Tab11")
        self.assertEqual(winners, ["BEATUSDT"])

    def test_blocklist_blocks_even_when_mode_off(self):
        server.state["symbol_filter_by_tab"]["Tab11"]["mode"] = "off"
        ok, reason = server._symbol_entry_allowed("Tab11", "UBUSDT")
        self.assertFalse(ok)
        self.assertIn("blocklist", reason)

    def test_auto_winners_allows_beatusdt(self):
        ok, reason = server._symbol_entry_allowed("Tab11", "BEATUSDT")
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_allowlist_requires_membership(self):
        server.state["symbol_filter_by_tab"]["Tab11"]["mode"] = "allowlist"
        server.state["symbol_allowlist_by_tab"]["Tab11"] = ["BEATUSDT"]
        ok, _ = server._symbol_entry_allowed("Tab11", "BEATUSDT")
        self.assertTrue(ok)
        ok, reason = server._symbol_entry_allowed("Tab11", "NEARUSDT")
        self.assertFalse(ok)
        self.assertIn("allowlist", reason)

    def test_leaderboard_marks_passes_filter(self):
        profit = server._symbol_leaderboard_rows("Tab11", limit=10, board="profit")
        loss = server._symbol_leaderboard_rows("Tab11", limit=10, board="loss")
        by_sym = {r["symbol"]: r for r in profit + loss}
        self.assertEqual([r["symbol"] for r in profit], ["BEATUSDT"])
        self.assertEqual([r["symbol"] for r in loss], ["UBUSDT"])
        self.assertTrue(by_sym["BEATUSDT"]["passes_filter"])
        self.assertFalse(by_sym["UBUSDT"]["passes_filter"])

    def test_leaderboard_splits_profit_and_loss(self):
        board = server._dashboard_symbol_leaderboard(limit=10)
        tab_board = board["Tab11"]
        self.assertEqual(len(tab_board["top_profit"]), 1)
        self.assertEqual(len(tab_board["top_loss"]), 1)
        self.assertGreater(tab_board["top_profit"][0]["net_pnl"], 0)
        self.assertLess(tab_board["top_loss"][0]["net_pnl"], 0)

    def test_rolling_window_forgets_old_losses(self):
        hist = self._history_closes("Tab11", "BEATUSDT", 0, 40, loss_pnl=-1.0)
        hist += self._history_closes("Tab11", "BEATUSDT", 20, 0, win_pnl=1.0)
        server.state["history"] = hist
        server._invalidate_rolling_symbol_stats_cache()
        # All-time: 20/60 wins (33%) — would fail. Last 30: 10L + 20W (67%) — passes.
        self.assertTrue(server._symbol_passes_auto_winners("Tab11", "BEATUSDT"))
        row = server._rolling_symbol_stats_for_tab("Tab11")["BEATUSDT"]
        self.assertEqual(row["trades"], 30)
        self.assertEqual(row["wins"], 20)


class LimitEntrySlotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.orig_state = copy.deepcopy(server.state)
        self.orig_live_mode = server.LIVE_MODE
        self.orig_entry_style = server.ENTRY_ORDER_STYLE
        self.orig_sltp_tp_style = server.SLTP_TP_STYLE
        self.orig_http_client = server._http_client
        server._http_client = object()
        server.LIVE_MODE = True
        server.ENTRY_ORDER_STYLE = "limit"
        server.SLTP_TP_STYLE = "limit"
        server.state = {
            "balances": {"Tab11": 10_000.0, "Tab18": 10_000.0},
            "unrealized_pnls": {"Tab11": 0.0, "Tab18": 0.0},
            "open_positions": {},
            "position_registry": {},
            "history": [],
            "used_setups": [],
            "sync_issues": [],
            "error_events": [],
            "pending_entry_orders": {},
            "max_positions_per_tab": 20,
        }

    def tearDown(self):
        server.state = self.orig_state
        server.LIVE_MODE = self.orig_live_mode
        server.ENTRY_ORDER_STYLE = self.orig_entry_style
        server.SLTP_TP_STYLE = self.orig_sltp_tp_style
        server._http_client = self.orig_http_client

    def test_tab_slot_count_includes_pending(self):
        server.state["open_positions"] = {
            f"SYM{i}_Tab11": {"tab": "Tab11", "symbol": f"SYM{i}", "side": "Long", "qty": 0.1}
            for i in range(19)
        }
        server.state["pending_entry_orders"] = {
            "SOLUSDT_Tab11": {"tab": "Tab11", "symbol": "SOLUSDT", "side": "Long", "qty": 0.1},
        }
        self.assertEqual(server._tab_open_slot_count("Tab11"), 20)
        self.assertEqual(server._tab_slots_remaining("Tab11"), 0)

    def test_max_full_skips_new_limit_placement(self):
        server.state["open_positions"] = {
            f"SYM{i}_Tab11": {"tab": "Tab11", "symbol": f"SYM{i}", "side": "Long", "qty": 1}
            for i in range(20)
        }
        self.assertEqual(server._tab_slots_remaining("Tab11"), 0)

    def test_shared_leg_two_tabs_separate_pending_limits(self):
        server.state["pending_entry_orders"] = {
            "BTCUSDT_Tab11": {
                "tab": "Tab11", "symbol": "BTCUSDT", "side": "Long",
                "position_side": "LONG", "qty": 0.1, "entry_order_id": 1001,
                "entry_client_order_id": "AG_Tab11_L_ENTRY_001",
            },
            "BTCUSDT_Tab18": {
                "tab": "Tab18", "symbol": "BTCUSDT", "side": "Long",
                "position_side": "LONG", "qty": 0.2, "entry_order_id": 1002,
                "entry_client_order_id": "AG_Tab18_L_ENTRY_002",
            },
        }
        self.assertEqual(len(server.state["pending_entry_orders"]), 2)
        self.assertNotEqual(
            server.state["pending_entry_orders"]["BTCUSDT_Tab11"]["entry_order_id"],
            server.state["pending_entry_orders"]["BTCUSDT_Tab18"]["entry_order_id"],
        )

    async def test_limit_entry_fill_promotes_to_open_and_places_protection(self):
        server.state["pending_entry_orders"]["BTCUSDT_Tab11"] = {
            "tab": "Tab11", "symbol": "BTCUSDT", "side": "Long", "position_side": "LONG",
            "setup_key": "BTCUSDT_Tab11_1710000000000",
            "signal_ep": 50000.0,
            "sig": {"side": "Long", "ep": 50000.0, "sl": 49000.0, "tp": 52000.0},
            "qty": 0.01, "filled_qty": 0.0,
            "entry_order_id": 9001, "entry_client_order_id": "AG_Tab11_L_ENTRY_test",
            "sl_local": False, "tp_local": False, "prot_reason": "", "use_local_protection": False,
        }
        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "_ensure_symbol_leverage", AsyncMock(return_value=5)),
            patch.object(server, "_fetch_sltp_trigger_price", AsyncMock(return_value=50000.0)),
            patch.object(server.binance_live, "place_stop_loss", AsyncMock(return_value={"algoId": 8001})) as place_sl,
            patch.object(server.binance_live, "place_exchange_take_profit", AsyncMock(return_value={"algoId": 8002})) as place_tp,
        ):
            await server.complete_limit_entry_fill("BTCUSDT_Tab11", 50000.0, 0.01)

        self.assertNotIn("BTCUSDT_Tab11", server.state["pending_entry_orders"])
        self.assertIn("BTCUSDT_Tab11", server.state["open_positions"])
        pos = server.state["open_positions"]["BTCUSDT_Tab11"]
        self.assertEqual(pos["qty"], 0.01)
        self.assertEqual(pos["sl_order_id"], 8001)
        self.assertEqual(pos["tp_order_id"], 8002)
        place_sl.assert_awaited_once()
        place_tp.assert_awaited_once()

    async def test_entry_limit_fill_routes_by_client_id_not_sibling(self):
        server.state["pending_entry_orders"] = {
            "BTCUSDT_Tab11": {
                "tab": "Tab11", "symbol": "BTCUSDT", "side": "Long", "position_side": "LONG",
                "qty": 0.01, "filled_qty": 0.0, "entry_order_id": 9001,
                "sig": {"side": "Long", "ep": 50000.0, "sl": 49000.0, "tp": 52000.0},
                "sl_local": True, "tp_local": True,
            },
            "BTCUSDT_Tab18": {
                "tab": "Tab18", "symbol": "BTCUSDT", "side": "Long", "position_side": "LONG",
                "qty": 0.02, "filled_qty": 0.0, "entry_order_id": 9002,
                "sig": {"side": "Long", "ep": 50000.0, "sl": 49000.0, "tp": 52000.0},
                "sl_local": True, "tp_local": True,
            },
        }
        complete = AsyncMock()
        with patch("bot.engine.entry.complete_limit_entry_fill", complete):
            await server.handle_order_update({
                "X": "FILLED", "i": 9001, "s": "BTCUSDT", "ps": "LONG", "S": "BUY",
                "o": "LIMIT", "c": "AG_Tab11_L_ENTRY_test", "ap": "50000", "z": "0.01",
            })
        complete.assert_awaited_once_with(
            "BTCUSDT_Tab11", 50000.0, 0.01, entry_order_id=9001,
        )
        self.assertIn("BTCUSDT_Tab18", server.state["pending_entry_orders"])

    async def test_tp_limit_partial_fill_reduces_tab_qty_on_shared_leg(self):
        server.state["open_positions"] = {
            "BTCUSDT_Tab11": {
                "tab": "Tab11", "symbol": "BTCUSDT", "side": "Long", "position_side": "LONG",
                "entry_price": 50000.0, "sl": 49000.0, "tp": 52000.0, "qty": 0.5,
                "sl_order_id": 101, "tp_order_id": 102,
            },
            "BTCUSDT_Tab18": {
                "tab": "Tab18", "symbol": "BTCUSDT", "side": "Long", "position_side": "LONG",
                "entry_price": 50000.0, "sl": 49000.0, "tp": 52000.0, "qty": 0.5,
                "sl_order_id": 201, "tp_order_id": 202,
            },
        }
        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server, "sync_live_positions", AsyncMock()),
            patch.object(server, "_close_position_unsafe", AsyncMock()) as close_pos,
        ):
            await server.handle_order_update({
                "X": "FILLED", "i": 102, "s": "BTCUSDT", "ps": "LONG", "S": "SELL",
                "o": "TAKE_PROFIT", "ap": "52000", "z": "0.3",
            })

        self.assertIn("BTCUSDT_Tab11", server.state["open_positions"])
        self.assertIn("BTCUSDT_Tab18", server.state["open_positions"])
        self.assertAlmostEqual(server.state["open_positions"]["BTCUSDT_Tab11"]["qty"], 0.2)
        self.assertEqual(server.state["open_positions"]["BTCUSDT_Tab11"]["tp_order_id"], None)
        close_pos.assert_not_awaited()

    async def test_sweep_preserves_sibling_tp_when_one_tab_closes(self):
        server.state["open_positions"] = {
            "BTCUSDT_Tab18": {
                "tab": "Tab18", "symbol": "BTCUSDT", "side": "Long", "position_side": "LONG",
                "qty": 0.5, "sl_order_id": 201, "tp_order_id": 202,
            },
        }
        algo_orders = [
            {"symbol": "BTCUSDT", "positionSide": "LONG", "algoId": 101, "orderType": "STOP_MARKET", "algoStatus": "NEW"},
            {"symbol": "BTCUSDT", "positionSide": "LONG", "algoId": 102, "orderType": "TAKE_PROFIT", "algoStatus": "NEW"},
            {"symbol": "BTCUSDT", "positionSide": "LONG", "algoId": 201, "orderType": "STOP_MARKET", "algoStatus": "NEW"},
            {"symbol": "BTCUSDT", "positionSide": "LONG", "algoId": 202, "orderType": "TAKE_PROFIT", "algoStatus": "NEW"},
        ]
        cancel_mock = AsyncMock()
        with (
            patch.object(server.binance_live, "_sreq", AsyncMock(return_value=algo_orders)),
            patch.object(server.binance_live, "cancel_algo_order", cancel_mock),
        ):
            cancelled = await server._sweep_algo_orders_for("BTCUSDT", "LONG")

        self.assertEqual(cancelled, 2)
        cancelled_ids = {call.kwargs.get("algo_id") for call in cancel_mock.await_args_list}
        self.assertEqual(cancelled_ids, {101, 102})

    async def test_trim_pending_on_max_positions_reduce(self):
        server.state["max_positions_per_tab"] = 20
        server.state["open_positions"] = {
            f"SYM{i}_Tab11": {"tab": "Tab11", "symbol": f"SYM{i}", "side": "Long", "qty": 0.1}
            for i in range(19)
        }
        server.state["pending_entry_orders"] = {
            "ETHUSDT_Tab11": {
                "tab": "Tab11", "symbol": "ETHUSDT", "side": "Long", "qty": 0.1,
                "entry_order_id": 7001, "placed_at": "2026-06-20T10:00:00+00:00",
            },
            "SOLUSDT_Tab11": {
                "tab": "Tab11", "symbol": "SOLUSDT", "side": "Long", "qty": 0.1,
                "entry_order_id": 7002, "placed_at": "2026-06-20T11:00:00+00:00",
            },
        }
        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server.binance_live, "cancel_order", AsyncMock()) as cancel_order,
        ):
            trimmed = await server._trim_pending_entries_for_tab("Tab11")

        self.assertEqual(trimmed, 1)
        self.assertIn("SOLUSDT_Tab11", server.state["pending_entry_orders"])
        self.assertNotIn("ETHUSDT_Tab11", server.state["pending_entry_orders"])
        cancel_order.assert_awaited_once()

    async def test_reconcile_pending_removes_stale_order(self):
        server.state["pending_entry_orders"] = {
            "BTCUSDT_Tab11": {
                "tab": "Tab11", "symbol": "BTCUSDT", "side": "Long", "qty": 0.01,
                "entry_order_id": 5555, "placed_at": "2026-06-20T10:00:00+00:00",
            },
        }
        with (
            patch.object(server, "save_state", AsyncMock()),
            patch.object(server.binance_live, "get_open_orders", AsyncMock(return_value=[])),
            patch.object(server, "_trim_pending_entries_for_tab", AsyncMock(return_value=0)),
        ):
            await server.reconcile_pending_entry_orders()

        self.assertNotIn("BTCUSDT_Tab11", server.state["pending_entry_orders"])


class HistoryPaginationTests(unittest.TestCase):
    def test_paginated_history_newest_first_with_offset(self):
        server.state["history"] = [
            {"tab": "Tab18", "symbol": "AAAUSDT", "side": "Long", "exit_time": "2026-01-01 00:00:00+00:00", "pnl_usd": 1.0, "exit_price": 1.0, "entry_price": 1.0, "reason": "TP"},
            {"tab": "Tab18", "symbol": "BBBUSDT", "side": "Short", "exit_time": "2026-06-01 00:00:00+00:00", "pnl_usd": 2.0, "exit_price": 2.0, "entry_price": 2.0, "reason": "SL"},
            {"tab": "Tab11", "symbol": "CCCUSDT", "side": "Long", "exit_time": "2026-06-02 00:00:00+00:00", "pnl_usd": 3.0, "exit_price": 3.0, "entry_price": 3.0, "reason": "TP"},
        ]
        page0 = server._paginated_history_page(tab="Tab18", offset=0, limit=1, days=0)
        self.assertEqual(page0["total"], 2)
        self.assertEqual(page0["history"][0]["symbol"], "BBBUSDT")
        self.assertTrue(page0["has_more"])
        page1 = server._paginated_history_page(tab="Tab18", offset=1, limit=1, days=0)
        self.assertEqual(page1["history"][0]["symbol"], "AAAUSDT")
        self.assertFalse(page1["has_more"])

    def test_equity_curve_api_all_time_downsample(self):
        server.state["history"] = [
            {
                "tab": "Tab18",
                "symbol": f"S{i}USDT",
                "side": "Long",
                "exit_time": f"2026-01-{i+1:02d} 00:00:00+00:00",
                "pnl_usd": 1.0,
                "exit_price": 1.0,
                "entry_price": 1.0,
                "reason": "TP",
            }
            for i in range(10)
        ]
        payload = server._dashboard_equity_curve_api(tab="Tab18", days=0, max_points=5)
        self.assertEqual(payload["total_closes"], 10)
        self.assertTrue(payload["downsampled"])
        self.assertLessEqual(payload["point_count"], 5)
        self.assertEqual(payload["series"][-1]["cumulative"], 10.0)
        self.assertIsNotNone(payload.get("max_drawdown"))
        self.assertAlmostEqual(payload["max_drawdown"]["usd"], 0.0)

    def test_equity_curve_max_drawdown_from_full_series_not_downsample(self):
        """Chart downsample must not change max_drawdown (computed before downsample)."""
        rows = []
        for i in range(100):
            pnl = -40.0 if i == 50 else 1.0
            rows.append({
                "tab": "Tab18",
                "symbol": f"S{i}USDT",
                "side": "Long",
                "exit_time": f"2026-01-{(i % 28) + 1:02d} 00:00:00+00:00",
                "pnl_usd": pnl,
                "exit_price": 1.0,
                "entry_price": 1.0,
                "reason": "TP" if pnl > 0 else "SL",
            })
        server.state["history"] = rows
        full = server._dashboard_equity_curve_api(tab="Tab18", days=0, max_points=0)
        sparse = server._dashboard_equity_curve_api(tab="Tab18", days=0, max_points=8)
        self.assertEqual(full["total_closes"], 100)
        self.assertTrue(sparse["downsampled"])
        self.assertIsNotNone(full.get("max_drawdown"))
        self.assertAlmostEqual(full["max_drawdown"]["usd"], sparse["max_drawdown"]["usd"])
        self.assertAlmostEqual(full["max_drawdown"]["pct"], sparse["max_drawdown"]["pct"])
        chart_mdd = _max_drawdown_from_equity_values([
            7000.0 + float(pt.get("cumulative") or 0)
            for pt in sparse["series"][1:]
            if pt.get("exit_time")
        ])
        self.assertNotAlmostEqual(chart_mdd["usd"], full["max_drawdown"]["usd"])

    def test_strategy_stats_api_matches_history_filter(self):
        server.state["history"] = [
            {"tab": "Tab18", "symbol": "AAAUSDT", "side": "Long", "exit_time": "2026-06-01 00:00:00+00:00", "pnl_usd": 5.0, "exit_price": 1.0, "entry_price": 1.0, "reason": "TP"},
            {"tab": "Tab18", "symbol": "BBBUSDT", "side": "Short", "exit_time": "2026-01-01 00:00:00+00:00", "pnl_usd": -2.0, "exit_price": 1.0, "entry_price": 1.0, "reason": "SL"},
            {"tab": "Tab11", "symbol": "CCCUSDT", "side": "Long", "exit_time": "2026-06-02 00:00:00+00:00", "pnl_usd": 3.0, "exit_price": 1.0, "entry_price": 1.0, "reason": "TP"},
        ]
        all_stats = server._dashboard_strategy_stats_api(days=0)
        self.assertEqual(all_stats["total_closes"], 3)
        self.assertEqual(all_stats["tab_stats"]["Tab18"]["trades"], 2)
        self.assertEqual(all_stats["tab_stats"]["Tab18"]["wins"], 1)
        self.assertAlmostEqual(all_stats["tab_stats"]["Tab18"]["grossWin"], 5.0)
        self.assertAlmostEqual(all_stats["tab_stats"]["Tab18"]["grossLoss"], 2.0)
        self.assertAlmostEqual(all_stats["side_pnl"]["Long"], 8.0)
        self.assertAlmostEqual(all_stats["side_pnl"]["Short"], -2.0)


if __name__ == "__main__":
    unittest.main()
