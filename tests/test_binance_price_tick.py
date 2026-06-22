"""Tests for Binance PRICE_FILTER tick rounding (-4014 prevention)."""

import unittest

import binance_live


class BinancePriceTickTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(binance_live.symbol_info)
        binance_live.symbol_info["BTWUSDT"] = {
            "qty_precision": 0,
            "price_precision": 7,
            "tick_size": 0.00001,
            "min_qty": 1.0,
            "min_notional": 5.0,
        }
        binance_live.symbol_info["CAKEUSDT"] = {
            "qty_precision": 0,
            "price_precision": 4,
            "tick_size": 0.001,
            "min_qty": 1.0,
            "min_notional": 5.0,
        }

    def tearDown(self):
        binance_live.symbol_info.clear()
        binance_live.symbol_info.update(self._saved)

    def test_round_price_tick_nearest(self):
        px = binance_live.round_price_tick("BTWUSDT", 0.1439631)
        self.assertEqual(px, 0.14396)

    def test_round_price_tick_long_tp_up(self):
        px = binance_live.round_price_tick("BTWUSDT", 0.1439631, mode="up")
        self.assertEqual(px, 0.14397)

    def test_format_price_uses_tick_not_precision_only(self):
        s = binance_live.format_price("BTWUSDT", 0.1439631)
        self.assertEqual(s, "0.14396")

    def test_cake_tp_rounds_to_mill(self):
        px = binance_live.round_price_tick("CAKEUSDT", 1.4046326, mode="up")
        self.assertEqual(px, 1.405)

    def test_ops_protection_risk_no_core_attr_error(self):
        from bot.engine.ops import _position_protection_risk

        risk = _position_protection_risk(
            "XRPUSDT_Tab18",
            {
                "symbol": "XRPUSDT",
                "tab": "Tab18",
                "side": "Long",
                "protection_status": "protection_failed",
                "protection_reason": "test",
            },
        )
        self.assertEqual(risk["level"], "critical")


if __name__ == "__main__":
    unittest.main()
