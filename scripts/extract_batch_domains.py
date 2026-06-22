"""Extract sync helpers, market feeds, and protection price helpers."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from domain_extract import apply_domain_extraction

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "bot" / "core.py"

CORE_STUB = '''
def _core():
    from bot import core
    return core


'''


def main() -> None:
    apply_domain_extraction(
        CORE,
        ROOT / "bot/engine/sync_helpers.py",
        import_module="bot.engine.sync_helpers",
        import_check="from bot.engine.sync_helpers import",
        start_marker="def _dt_to_ms",
        end_marker='print("[Startup] Loading modules complete',
        header='''"""Exchange sync close backfill (extracted from bot.core)."""

from __future__ import annotations

from datetime import datetime, timezone

import binance_live
''' + CORE_STUB,
        core_attrs=("_http_client", "_daily_loss_usd", "_circuit_breaker"),
        config_via_core=("LIVE_MODE", "HISTORY_CAP", "CIRCUIT_BREAKER_DAILY_LOSS"),
        insert_before='print("[Startup] Loading modules complete',
    )

    apply_domain_extraction(
        CORE,
        ROOT / "bot/feeds/market.py",
        import_module="bot.feeds.market",
        import_check="from bot.feeds.market import",
        start_marker="_EXCLUDE_SYMBOLS = {",
        end_marker="def _protection_prices_from_entry",
        header='''"""Price feed, scan universe, and mark price helpers."""

from __future__ import annotations

import asyncio
import time

import binance_live
from config import (
    KLINE_FETCH_DELAY_SEC,
    KLINE_FETCH_MIN_DELAY_SEC,
    MAX_FUNDING_RATE_ABS,
    MAX_SPREAD_PCT,
    PRICE_FEED_BASE_URL,
    PRICE_FEED_ENV,
    SLIPPAGE_PCT,
)
''' + CORE_STUB,
        core_attrs=(
            "SCAN_SYMBOLS",
            "SCAN_TICKER_BY_SYM",
            "latest_prices",
            "latest_marks",
            "_last_mark_refresh_at",
            "_http_client",
            "_PAPER_MARK_REFRESH_INTERVAL_SEC",
        ),
        config_via_core=(
            "PRICE_FEED_BASE_URL",
            "PRICE_FEED_ENV",
            "MAX_FUNDING_RATE_ABS",
            "MAX_SPREAD_PCT",
            "KLINE_FETCH_DELAY_SEC",
            "KLINE_FETCH_MIN_DELAY_SEC",
            "SLIPPAGE_PCT",
        ),
        insert_before="def _protection_prices_from_entry",
    )

    apply_domain_extraction(
        CORE,
        ROOT / "bot/engine/protection_prices.py",
        import_module="bot.engine.protection_prices",
        import_check="from bot.engine.protection_prices import",
        start_marker="def _protection_prices_from_entry",
        end_marker="def _sltp_diff_pct_from_entry",
        header='''"""SL/TP price planning from entry fill (extracted from bot.core)."""

from __future__ import annotations

import binance_live
from config import EXCHANGE_MARK_NUDGE_PCT, MAX_SL_PCT
''' + CORE_STUB,
        config_via_core=("EXCHANGE_MARK_NUDGE_PCT", "MAX_SL_PCT", "MARK_FILL_SANITY_PCT", "MAX_EXCHANGE_PROTECTION_NUDGE_PCT"),
        insert_before="def _sltp_diff_pct_from_entry",
    )


if __name__ == "__main__":
    main()
