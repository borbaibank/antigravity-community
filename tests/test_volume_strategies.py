import os
import sys
import unittest

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config
import strategies


def _flat_ohlcv(length=230):
    rows = []
    for i in range(length):
        rows.append({
            "timestamp": i,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1000.0,
        })
    return pd.DataFrame(rows)


class VolumeStrategyTests(unittest.TestCase):
    def test_volume_tabs_run_on_1h(self):
        self.assertEqual(config.TAB_TIMEFRAMES["Tab5"], "1h")
        self.assertEqual(config.TAB_TIMEFRAMES["Tab8"], "1h")
        self.assertEqual(config.TAB_TIMEFRAMES["Tab9"], "1h")
        self.assertEqual(config.TAB_TIMEFRAMES["Tab10"], "1h")


    def test_tab10_range_expansion_spike_long(self):
        df = _flat_ohlcv()
        df.loc[len(df) - 2, ["open", "high", "low", "close", "volume"]] = [100.0, 104.0, 99.0, 103.0, 2000.0]
        df.loc[len(df) - 1, "open"] = 103.2

        sig = strategies.evaluate_tab10_vol_range_expansion_spike(df)

        self.assertIsNotNone(sig)
        self.assertEqual(sig["side"], "Long")
        self.assertEqual(sig["reason"], "Tab10_VolRangeSpike")
        self.assertAlmostEqual(sig["ep"], 103.2)
        self.assertLess(sig["sl"], sig["ep"])
        self.assertAlmostEqual(sig["tp"], sig["ep"] + (sig["ep"] - sig["sl"]) * 1.5)









