"""Extract exit, entry, ws feeds, sync, and scheduler domains from bot/core.py."""

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

EXIT_CORE_ATTRS = (
    "_state_lock",
    "_http_client",
    "_BINANCE_RATE_LIMIT_UNTIL_MS",
    "_BINANCE_RATE_LIMIT_REASON",
    "_BINANCE_RATE_LIMIT_ALERT_SENT",
    "_daily_loss_usd",
    "_circuit_breaker",
    "_daily_loss_date",
    "_last_live_close_mono",
    "latest_marks",
    "latest_prices",
)

EXIT_CONFIG = (
    "LIVE_MODE",
    "CIRCUIT_BREAKER_DAILY_LOSS",
    "BINANCE_IP_BAN_DEFAULT_BACKOFF_SEC",
    "PNL_REPAIR_STARTUP_DELAY_SEC",
    "PNL_REPAIR_BATCH_SIZE",
    "PNL_REPAIR_BATCH_PAUSE_SEC",
    "PNL_REPAIR_ENTRY_DELAY_SEC",
    "PNL_REPAIR_DEFER_POLL_SEC",
    "CLOSE_ALL_STAGGER_SEC",
    "CLOSE_ALL_PREFLIGHT",
    "CLOSE_ALL_RETRY_SEC",
    "EXIT_FEE_MAKER_PCT",
    "EXIT_FEE_TAKER_PCT",
    "SLIPPAGE_PCT",
    "TABS",
    "HISTORY_CAP",
    "ENTRY_FEE_PCT",
    "BINANCE_CLOSE_HISTORY_ENABLED",
    "BINANCE_TESTNET",
)

ENTRY_CORE_ATTRS = (
    "_state_lock",
    "_eval_lock",
    "_http_client",
    "_pending_entry_retries",
    "_ENTRY_STAGGER_ARMED",
    "_entry_busy_until_mono",
    "_entry_busy_defer_log_at",
    "_circuit_breaker",
    "_daily_loss_usd",
    "exchange_account",
    "latest_marks",
    "latest_prices",
    "SCAN_SYMBOLS",
    "SCAN_TICKER_BY_SYM",
)

ENTRY_CONFIG = (
    "LIVE_MODE",
    "TABS",
    "TAB_TIMEFRAMES",
    "STARTUP_ENABLED_TABS",
    "MAX_POSITIONS_PER_TAB",
    "NOTIONAL_SIZE",
    "LEVERAGE",
    "SYMBOL_SCAN_LIMIT",
    "ENTRY_FEE_PCT",
    "ENTRY_STAGGER_SEC",
    "ENTRY_EVAL_BUDGET_SEC",
    "ENTRY_BUSY_BUFFER_SEC",
    "ENTRY_4192_RETRY_DELAY_SEC",
    "ENTRY_4192_PRICE_POLL_SEC",
    "ENTRY_4192_MAX_RETRIES",
    "ENTRY_4192_RETRY_MAX_AGE_SEC",
    "ENTRY_WAIT_FOR_BETTER_PRICE",
    "ENTRY_PRICE_WAIT_MAX_SEC",
    "ENTRY_PRICE_POLL_SEC",
    "MAX_ENTRY_SIGNAL_DRIFT_PCT",
    "KLINE_FETCH_DELAY_SEC",
    "CIRCUIT_BREAKER_DAILY_LOSS",
    "TAB17_BASE_UNIVERSE",
    "TAB17_MOMENTUM_TOP_N",
    "TAB17_MIN_PRICE_CHG_PCT",
    "TAB17_MIN_VOL_SPIKE_MULT",
    "TAB17_VOL_SMA_LEN",
    "TAB17_VOL_RATIO_CAP",
    "TAB17_MAX_POS",
    "MIN_ENTRY_AVAILABLE_MARGIN",
    "ENTRY_LOCAL_SL_GRACE_SEC",
)

WS_CORE_ATTRS = (
    "_http_client",
    "latest_marks",
    "latest_prices",
    "SCAN_SYMBOLS",
    "_last_price_ws_ok_at",
)

WS_CONFIG = (
    "LIVE_MODE",
    "PRICE_FEED_WS_URL",
    "PRICE_FEED_BASE_URL",
)

WS_MODULE_ATTRS = (
    "_WS_SYMBOL_CACHE_SEC",
    "_DASHBOARD_MARKET_SYMBOLS",
    "_PRICE_WS_READ_TIMEOUT_SEC",
    "_PRICE_WS_PING_INTERVAL_SEC",
    "_PRICE_WS_PING_TIMEOUT_SEC",
    "_PRICE_TICK_MONITOR_INTERVAL_SEC",
    "_PRICE_TICK_MONITOR_INTERVAL_HYBRID_SEC",
)

SYNC_CORE_ATTRS = (
    "_state_lock",
    "_http_client",
    "_uds_connected",
    "_last_uds_connected_at",
    "_last_uds_error_at",
    "_last_sync_ok_at",
    "_last_sync_error_at",
    "_last_exchange_account_ok_at",
    "_dashboard_algo_order_count",
    "_last_uds_account_update_mono",
    "_symbol_leverage",
    "_SYNC_ENTRY_GRACE_SEC",
    "exchange_account",
    "latest_marks",
    "latest_prices",
)

SYNC_CONFIG = (
    "LIVE_MODE",
    "UDS_ACCOUNT_FRESH_SEC",
    "EXCHANGE_ACCOUNT_POLL_SEC_UDS",
    "EXCHANGE_ACCOUNT_POLL_SEC_NO_UDS",
    "EXCHANGE_ACCOUNT_SYNC_SEC_UDS",
    "EXCHANGE_ACCOUNT_SYNC_SEC_NO_UDS",
    "EXCHANGE_ACCOUNT_SYNC_SEC_UDS_CONNECTED",
    "LOW_MARGIN_THRESHOLD",
    "TABS",
)

SCHED_CORE_ATTRS = (
    "_http_client",
    "_eval_lock",
    "_circuit_breaker",
    "_daily_loss_usd",
    "_daily_loss_date",
    "_last_live_close_mono",
    "_last_income_today_sync_mono",
    "_pionex_balance_snapshot",
    "exchange_account",
    "_INCOME_SYNC_POLL_SEC",
    "_INCOME_SYNC_POLL_ACTIVE_SEC",
    "_INCOME_SYNC_IDLE_AFTER_CLOSE_SEC",
    "_INCOME_TODAY_REFRESH_SEC",
    "_INCOME_DAILY_30D_REFRESH_SEC",
    "_MARGIN_HISTORY_CAP",
)

SCHED_CONFIG = (
    "LIVE_MODE",
    "PIONEX_CONFIGURED",
    "PIONEX_BALANCE_POLL_SEC",
    "ENTRY_EVAL_BUDGET_SEC",
    "ENTRY_BUSY_BUFFER_SEC",
    "KLINE_FETCH_DELAY_SEC",
    "TABS",
)


def main() -> None:
    apply_domain_extraction(
        CORE,
        ROOT / "bot/engine/exit.py",
        import_module="bot.engine.exit",
        import_check="from bot.engine.exit import",
        start_marker="def _close_side_for_position",
        end_marker="async def check_invalidations_loop",
        header='''"""Position close, PnL reconcile, and rate-limit helpers (extracted from bot.core)."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

import binance_live
from config import (
    BINANCE_IP_BAN_DEFAULT_BACKOFF_SEC,
    CIRCUIT_BREAKER_DAILY_LOSS,
    CLOSE_ALL_PREFLIGHT,
    CLOSE_ALL_RETRY_SEC,
    CLOSE_ALL_STAGGER_SEC,
    EXIT_FEE_MAKER_PCT,
    EXIT_FEE_TAKER_PCT,
    PNL_REPAIR_BATCH_PAUSE_SEC,
    PNL_REPAIR_BATCH_SIZE,
    PNL_REPAIR_DEFER_POLL_SEC,
    PNL_REPAIR_ENTRY_DELAY_SEC,
    PNL_REPAIR_STARTUP_DELAY_SEC,
    SLIPPAGE_PCT,
)
''' + CORE_STUB,
        core_attrs=EXIT_CORE_ATTRS,
        config_via_core=EXIT_CONFIG,
        insert_before="async def check_invalidations_loop",
        preserve_nested_calls=True,
    )

    apply_domain_extraction(
        CORE,
        ROOT / "bot/engine/entry.py",
        import_module="bot.engine.entry",
        import_check="from bot.engine.entry import",
        start_marker="async def check_invalidations_loop",
        end_marker="def _ws_price_symbol_filter",
        header='''"""Signal scan, entry execution, and entry retry queue (extracted from bot.core)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import binance_live
import strategies
from bot.engine.signals_registry import TAB_EVALUATORS_1H, TAB_EVALUATORS_4H, evaluate_tab_signal
from config import (
    CIRCUIT_BREAKER_DAILY_LOSS,
    ENTRY_4192_MAX_RETRIES,
    ENTRY_4192_PRICE_POLL_SEC,
    ENTRY_4192_RETRY_DELAY_SEC,
    ENTRY_4192_RETRY_MAX_AGE_SEC,
    ENTRY_BUSY_BUFFER_SEC,
    ENTRY_EVAL_BUDGET_SEC,
    ENTRY_FEE_PCT,
    ENTRY_PRICE_POLL_SEC,
    ENTRY_PRICE_WAIT_MAX_SEC,
    ENTRY_STAGGER_SEC,
    ENTRY_WAIT_FOR_BETTER_PRICE,
    KLINE_FETCH_DELAY_SEC,
    LEVERAGE,
    MAX_ENTRY_SIGNAL_DRIFT_PCT,
    MIN_ENTRY_AVAILABLE_MARGIN,
    MAX_POSITIONS_PER_TAB,
    NOTIONAL_SIZE,
    STARTUP_ENABLED_TABS,
    SYMBOL_SCAN_LIMIT,
    TAB17_BASE_UNIVERSE,
    TAB17_MOMENTUM_TOP_N,
    TAB_TIMEFRAMES,
)
''' + CORE_STUB,
        core_attrs=ENTRY_CORE_ATTRS,
        config_via_core=ENTRY_CONFIG,
        insert_before="def _ws_price_symbol_filter",
        preserve_nested_calls=True,
    )

    apply_domain_extraction(
        CORE,
        ROOT / "bot/feeds/ws.py",
        import_module="bot.feeds.ws",
        import_check="from bot.feeds.ws import",
        start_marker="def _ws_price_symbol_filter",
        end_marker="async def handle_order_update",
        header='''"""Binance price WebSocket and tick monitor (extracted from bot.core)."""

from __future__ import annotations

import asyncio
import json

import websockets
from config import PRICE_FEED_WS_URL
''' + CORE_STUB,
        core_attrs=WS_CORE_ATTRS + WS_MODULE_ATTRS,
        config_via_core=WS_CONFIG,
        insert_before="async def handle_order_update",
        preserve_nested_calls=True,
    )

    apply_domain_extraction(
        CORE,
        ROOT / "bot/engine/sync.py",
        import_module="bot.engine.sync",
        import_check="from bot.engine.sync import",
        start_marker="async def handle_order_update",
        end_marker="async def scheduler_loop",
        header='''"""Live position sync, UDS, and order update handlers (extracted from bot.core)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import binance_live
import websockets
from config import (
    EXCHANGE_ACCOUNT_POLL_SEC_NO_UDS,
    EXCHANGE_ACCOUNT_POLL_SEC_UDS,
    EXCHANGE_ACCOUNT_SYNC_SEC_NO_UDS,
    EXCHANGE_ACCOUNT_SYNC_SEC_UDS,
    EXCHANGE_ACCOUNT_SYNC_SEC_UDS_CONNECTED,
    UDS_ACCOUNT_FRESH_SEC,
)
''' + CORE_STUB,
        core_attrs=SYNC_CORE_ATTRS,
        config_via_core=SYNC_CONFIG,
        accessor_via_core=(
            "STRATEGY_LABELS",
            "_FALLBACK_LOCAL_REASON",
            "_MAINNET_LOCAL_SLTP_REASON",
            "_HYBRID_SLTP_REASON",
            "_BINANCE_EXCHANGE_REASON",
        ),
        insert_before="async def scheduler_loop",
        preserve_nested_calls=True,
    )

    apply_domain_extraction(
        CORE,
        ROOT / "bot/scheduler/loops.py",
        import_module="bot.scheduler.loops",
        import_check="from bot.scheduler.loops import",
        start_marker="async def scheduler_loop",
        end_marker="# --- FASTAPI APP ---",
        header='''"""Background scheduler and income polling loops (extracted from bot.core)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import binance_live
import pionex_live
from config import (
    ENTRY_BUSY_BUFFER_SEC,
    ENTRY_EVAL_BUDGET_SEC,
    KLINE_FETCH_DELAY_SEC,
    PIONEX_BALANCE_POLL_SEC,
)
''' + CORE_STUB,
        core_attrs=SCHED_CORE_ATTRS,
        config_via_core=SCHED_CONFIG,
        insert_before="# --- FASTAPI APP ---",
        preserve_nested_calls=True,
    )


if __name__ == "__main__":
    main()
