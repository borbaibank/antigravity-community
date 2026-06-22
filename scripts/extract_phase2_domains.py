"""Extract history, PnL/income, and klines domains from bot/core.py."""

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
        ROOT / "bot/engine/history.py",
        import_module="bot.engine.history",
        import_check="from bot.engine.history import",
        start_marker="def _sltp_diff_pct_from_entry",
        end_marker="def _validate_planned_protection",
        header='''"""Trade history, SL/TP diff helpers, and dashboard history (extracted from bot.core)."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import binance_live
from config import (
    BINANCE_CLOSE_HISTORY_BATCH_SIZE,
    BINANCE_CLOSE_HISTORY_DAYS,
    BINANCE_CLOSE_HISTORY_REFRESH_SYMBOL_CAP,
    BINANCE_CLOSE_HISTORY_SYMBOL_DELAY_SEC,
    BINANCE_CLOSE_HISTORY_TTL_SEC,
    HISTORY_CAP,
    UDS_ACCOUNT_FRESH_SEC,
)
''' + CORE_STUB,
        core_attrs=(
            "_http_client",
            "_BINANCE_CLOSE_HISTORY_CACHE",
            "_BINANCE_RAW_TRADES_CACHE",
            "_BINANCE_TRADES_BY_SYMBOL",
            "_BINANCE_HISTORY_SYMBOL_INDEX",
            "_BINANCE_CLOSE_HISTORY_FETCHED_AT",
            "_BINANCE_CLOSE_HISTORY_LOCK",
            "exchange_account",
            "SCAN_SYMBOLS",
            "_symbol_leverage",
            "_last_uds_account_update_mono",
            "_last_exchange_account_ok_at",
        ),
        config_via_core=(
            "LIVE_MODE",
            "LOW_MARGIN_THRESHOLD",
            "BINANCE_CLOSE_HISTORY_ENABLED",
            "BINANCE_CLOSE_HISTORY_DAYS",
            "BINANCE_CLOSE_HISTORY_SYMBOL_CAP",
            "BINANCE_CLOSE_HISTORY_TTL_SEC",
            "BINANCE_CLOSE_HISTORY_REFRESH_SYMBOL_CAP",
            "BINANCE_CLOSE_HISTORY_BATCH_SIZE",
            "BINANCE_CLOSE_HISTORY_SYMBOL_DELAY_SEC",
            "HISTORY_CAP",
            "UDS_ACCOUNT_FRESH_SEC",
            "TABS",
        ),
        insert_before="def _validate_planned_protection",
        preserve_nested_calls=True,
    )

    apply_domain_extraction(
        CORE,
        ROOT / "bot/engine/pnl.py",
        import_module="bot.engine.pnl",
        import_check="from bot.engine.pnl import",
        start_marker="def _position_mark_price",
        end_marker="async def _check_local_position_exits_unsafe",
        header='''"""Unrealized PnL, income sync, and dashboard PnL helpers (extracted from bot.core)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import binance_live
from config import (
    EQUITY_CURVE_MARGIN_BASELINE,
    INITIAL_BALANCE,
    LOW_MARGIN_THRESHOLD,
    PIONEX_CONFIGURED,
)
''' + CORE_STUB,
        core_attrs=(
            "_http_client",
            "exchange_account",
            "_today_binance_profit",
            "_daily_profit_30d",
            "_last_daily_profit_30d_sync_mono",
            "_last_income_today_sync_mono",
            "_last_live_close_mono",
            "_pionex_balance_snapshot",
            "latest_marks",
            "latest_prices",
        ),
        config_via_core=(
            "LIVE_MODE",
            "TABS",
            "INITIAL_BALANCE",
            "EQUITY_CURVE_MARGIN_BASELINE",
            "LOW_MARGIN_THRESHOLD",
            "PIONEX_CONFIGURED",
        ),
        insert_before="async def _check_local_position_exits_unsafe",
        preserve_nested_calls=True,
    )

    apply_domain_extraction(
        CORE,
        ROOT / "bot/feeds/klines.py",
        import_module="bot.feeds.klines",
        import_check="from bot.feeds.klines import",
        start_marker="async def get_klines",
        end_marker="async def check_invalidations_loop",
        header='''"""Kline REST fetch (extracted from bot.core)."""

from __future__ import annotations

import asyncio

from config import KLINE_FETCH_CONCURRENCY, PRICE_FEED_BASE_URL
''' + CORE_STUB + '''
_KLINE_FETCH_SEM = asyncio.Semaphore(max(1, KLINE_FETCH_CONCURRENCY))

''',
        core_attrs=("_http_client",),
        config_via_core=("PRICE_FEED_BASE_URL",),
        insert_before="async def check_invalidations_loop",
    )


def _remove_stray_kline_sem() -> None:
    """Git restore leaves a duplicate semaphore line before get_klines."""
    text = CORE.read_text(encoding="utf-8")
    stray = "_KLINE_FETCH_SEM = asyncio.Semaphore(max(1, KLINE_FETCH_CONCURRENCY))\n\n"
    if stray in text:
        CORE.write_text(text.replace(stray, "", 1), encoding="utf-8")
        print("Removed stray _KLINE_FETCH_SEM from core.py")


if __name__ == "__main__":
    main()
    _remove_stray_kline_sem()
