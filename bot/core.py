"""Trading engine core — extracted from server.py (behavior-preserving)."""
from __future__ import annotations

import strategies

try:
    from antigravity_pro import register as _register_antigravity_pro
    _register_antigravity_pro()
except ImportError:
    pass

import asyncio
import atexit
import builtins
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import sys
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
import websockets
import httpx
import pandas as pd
from fastapi import FastAPI, WebSocket, Request, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketDisconnect
import uvicorn
import binance_live
import pionex_live
from bot.engine.signals_registry import TAB_EVALUATORS_1H, TAB_EVALUATORS_4H, evaluate_tab_signal
from bot.state.schema import default_state, default_state_corrupt_recovery
from config import (
    INITIAL_BALANCE, NOTIONAL_SIZE, MARGIN_SIZE,
    ENTRY_FEE_PCT, EXIT_FEE_MAKER_PCT, EXIT_FEE_TAKER_PCT, SLIPPAGE_PCT,
    MAX_POSITIONS_PER_TAB, SYMBOL_SCAN_LIMIT, KLINE_FETCH_CONCURRENCY, STARTUP_ENABLED_TABS,
    HISTORY_CAP, USED_SETUPS_CAP, MAX_SL_PCT,
    BINANCE_CLOSE_HISTORY_ENABLED,
    BINANCE_CLOSE_HISTORY_DAYS, BINANCE_CLOSE_HISTORY_SYMBOL_CAP,
    DASHBOARD_WS_HISTORY_LIMIT, DASHBOARD_HISTORY_PAGE_MAX,
    DASHBOARD_EQUITY_CURVE_MAX_POINTS,
    BINANCE_CLOSE_HISTORY_TTL_SEC, BINANCE_CLOSE_HISTORY_REFRESH_SYMBOL_CAP,
    BINANCE_CLOSE_HISTORY_BATCH_SIZE, BINANCE_CLOSE_HISTORY_SYMBOL_DELAY_SEC,
    EXCHANGE_ACCOUNT_POLL_SEC_UDS, EXCHANGE_ACCOUNT_POLL_SEC_NO_UDS,
    EXCHANGE_ACCOUNT_SYNC_SEC_UDS, EXCHANGE_ACCOUNT_SYNC_SEC_NO_UDS,
    EXCHANGE_ACCOUNT_SYNC_SEC_UDS_CONNECTED, UDS_ACCOUNT_FRESH_SEC,
    PNL_REPAIR_STARTUP_DELAY_SEC, PNL_REPAIR_BATCH_SIZE,
    PNL_REPAIR_BATCH_PAUSE_SEC, PNL_REPAIR_ENTRY_DELAY_SEC, PNL_REPAIR_DEFER_POLL_SEC,
    ENTRY_EVAL_BUDGET_SEC, ENTRY_BUSY_BUFFER_SEC,
    KLINE_FETCH_DELAY_SEC, KLINE_FETCH_MIN_DELAY_SEC, ENTRY_LOCAL_SL_GRACE_SEC,
    ENTRY_STAGGER_SEC, CLOSE_ALL_STAGGER_SEC, CLOSE_ALL_PREFLIGHT,
    CLOSE_ALL_RETRY_SEC,
    ENTRY_4192_RETRY_DELAY_SEC, ENTRY_4192_PRICE_POLL_SEC, ENTRY_4192_MAX_RETRIES,
    ENTRY_4192_RETRY_MAX_AGE_SEC,
    ENTRY_WAIT_FOR_BETTER_PRICE, ENTRY_PRICE_WAIT_MAX_SEC, ENTRY_PRICE_POLL_SEC,
    ENTRY_MIN_PRICE_IMPROVE_PCT,
    BINANCE_IP_BAN_DEFAULT_BACKOFF_SEC,
    TAB_TIMEFRAMES, TABS,
    LIVE_MODE, LEVERAGE, BINANCE_TESTNET, LOCAL_SLTP, SLTP_MODE, SLTP_MODES, BINANCE_API_ACCOUNT_TYPE, LOW_MARGIN_THRESHOLD, MIN_ENTRY_AVAILABLE_MARGIN,
    ORDER_ENV, PRICE_FEED_ENV, PRICE_FEED_BASE_URL, PRICE_FEED_WS_URL,
    DASHBOARD_PASSCODE, DASHBOARD_AUTH_ENABLED, DASHBOARD_ALLOWED_ORIGINS, DASHBOARD_PORT, EQUITY_CURVE_MARGIN_BASELINE,
    CIRCUIT_BREAKER_DAILY_LOSS,
    TELEGRAM_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    PIONEX_CONFIGURED, PIONEX_BALANCE_POLL_SEC,
    MAX_FUNDING_RATE_ABS, MAX_SPREAD_PCT, MAX_ENTRY_SIGNAL_DRIFT_PCT, MAX_NOTIONAL_SIZE,
    MARK_FILL_SANITY_PCT, EXCHANGE_MARK_NUDGE_PCT, MAX_EXCHANGE_PROTECTION_NUDGE_PCT,
    SLTP_TRIGGER_PRICE, ALGO_WORKING_TYPE,
    ENTRY_TRIGGER_PRICE,
    ENTRY_ORDER_STYLE, ENTRY_LIMIT_TIF, ENTRY_LIMIT_MAX_AGE_SEC,
    SLTP_TP_STYLE, ENTRY_LIMIT_TP_PRICE_MODE,
    SYMBOL_FILTER_MODES,
    SYMBOL_FILTER_DEFAULT_MIN_TRADES,
    SYMBOL_FILTER_DEFAULT_MIN_WIN_RATE,
    SYMBOL_FILTER_DEFAULT_MIN_NET_PNL,
    SYMBOL_FILTER_ROLLING_WINDOW,
    TAB_TIMEFRAMES, TABS,
)

from bot import logging_setup as _logmod

_log_buffer = _logmod._log_buffer
LOG_ARCHIVE_DIR = _logmod.LOG_ARCHIVE_DIR
_log_entry_open = _logmod._log_entry_open
_log_exit_close = _logmod._log_exit_close
_log_price = _logmod._log_price
_exit_move_pct_from_entry = _logmod._exit_move_pct_from_entry
_exit_target_slip_from_fill = _logmod._exit_target_slip_from_fill
_utc_log_stamp = _logmod._utc_log_stamp
_configure_library_loggers = _logmod._configure_library_loggers


def _print_startup_banner() -> None:
    _logmod.print_startup_banner(
        effective_sltp_mode=_effective_sltp_mode,
        max_open_algo_orders=_max_open_algo_orders,
        effective_kline_fetch_delay_sec=_effective_kline_fetch_delay_sec,
        tg_tp_close=TG_TP_CLOSE,
        tg_profit_close=TG_PROFIT_CLOSE,
        tg_loss_close=TG_LOSS_CLOSE,
    )


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- PAPER TRADING STATE ---
STATE_FILE = "paper_state.json"
STATE_ARCHIVE_DIR = os.path.join(_PROJECT_ROOT, "archive", "paper_state")
os.makedirs(STATE_ARCHIVE_DIR, exist_ok=True)
state = {}
STARTUP_ENABLED_TABS = set(STARTUP_ENABLED_TABS)
MAX_POSITIONS_OPTIONS = (10, 20, 30, 40, 50, 60, 70, 80)
NOTIONAL_SIZE_OPTIONS = (
    10.0, 20.0, 30.0, 40.0, 60.0, 100.0, 150.0, 200.0,
    400.0, 800.0, 1600.0, 3200.0,
)
LEVERAGE_OPTIONS = (1, 2, 3, 5, 10, 20)
MARGIN_SIZE_OPTIONS = (2.0, 4.0, 6.0, 8.0, 12.0, 20.0, 30.0, 40.0, 80.0, 160.0, 320.0, 640.0)
MIN_MARGIN_SIZE = 1.0
MAX_MARGIN_SIZE = 1000.0
SYMBOL_SCAN_OPTIONS = (100, 200, 300, 400, 500)
_DEFAULT_MARGIN_SIZE = MARGIN_SIZE


from bot.state.accessors import (
    _match_float_option,
    _effective_leverage,
    _effective_margin_size,
    _effective_max_positions,
    _effective_long_short_balance_mode,
    _effective_trade_side_mode,
    _effective_notional_size,
    _notional_cap_blocked,
    _sync_sizing_state,
    _normalize_sizing_state,
    _effective_symbol_scan_limit,
    _clamp_symbol_scan_limit,
    _normalize_symbol_scan_limit_by_tab,
    _effective_symbol_scan_limit_for_tab,
    _scan_universe_size,
    _interval_has_enabled_tabs,
    _symbols_base_universe,
    _symbols_for_interval_scan,
    _kline_request_weight,
    _MAINNET_LOCAL_SLTP_REASON,
    _BINANCE_EXCHANGE_REASON,
    _HYBRID_SLTP_REASON,
    _FALLBACK_LOCAL_REASON,
    _LOCAL_SLTP_POLICY_REASONS,
    _normalize_sltp_mode,
    _default_sltp_mode_from_state_fields,
    _effective_sltp_mode,
    _effective_local_sltp,
    _max_open_algo_orders,
    _startup_tab_enabled,
    _normalize_tab_enabled,
    _TAB_STATS_VERSION,
    _is_winning_trade,
    _empty_tab_stats_row,
    _normalize_tab_stats,
    _accumulate_tab_stats_row,
    _rebuild_tab_stats_from_history,
    _record_tab_stats_close,
    _SYMBOL_STATS_VERSION,
    _empty_symbol_stats_row,
    _accumulate_symbol_stats_row,
    _normalize_symbol_stats,
    _record_symbol_stats_close,
    _rebuild_symbol_stats_from_history,
    _invalidate_rolling_symbol_stats_cache,
    _rolling_symbol_stats_cache_key,
    _all_rolling_symbol_stats,
    _rolling_symbol_stats_for_tab,
    _default_symbol_filter_row,
    _clamp_symbol_filter_row,
    _normalize_symbol_filter_by_tab,
    _effective_symbol_filter,
    _normalize_symbol_list_by_tab,
    _symbol_in_blocklist,
    _symbol_passes_auto_winners,
    _auto_winner_symbols,
    _symbol_passes_filter,
    _symbol_entry_allowed,
    _symbol_leaderboard_rows,
    _dashboard_symbol_leaderboard,
    _rebuild_tab_stats_for_tab,
    _binance_close_history_enabled,
    _use_binance_close_cache,
    _rebuild_tab_stats_from_binance_closes,
)

from bot.engine.protection import (
    _position_sl_is_local,
    _position_tp_is_local,
    _vanished_exchange_protection_reason,
    _position_full_local,
    _position_needs_local_exit_monitor,
    _resolve_entry_protection_plan,
    _apply_protection_sources,
    _policy_cancels_untracked_exchange_algos,
)

from bot.state.persistence import (
    _state_write_guard_allows_save,
    _state_write_lock,
    _write_state,
    load_state,
    save_state,
)


from bot.engine.ops import (
    _is_tp_sl_exit_reason,
    _telegram_allowed,
    send_telegram,
    _iso_age_seconds,
    _mark_heartbeat,
    record_error_event,
    _expected_local_protection,
    _position_protection_risk,
    _position_protection_risks,
    _health_snapshot,
    _ERROR_EVENTS_CAP,
    _ERROR_EVENT_DEDUP_SEC,
    _error_event_recent,
)

from bot.state.position_identity import (
    _algo_id,
    _algo_trigger_price,
    _algo_quantity,
    _algo_order_type,
    _is_active_algo_order,
    _active_algo_orders,
    _verified_active_algo_orders,
    _position_side_name,
    _position_side_from_state,
    _position_tuple_from_state,
    _position_tuple_from_exchange,
    STRATEGY_LABELS,
    TG_OPEN,
    TG_ENTRY,
    TG_TP_CLOSE,
    TG_PROFIT_CLOSE,
    TG_LOSS_CLOSE,
    _telegram_exit_icon,
    _strategy_label,
    _strategy_client_id,
    _strategy_from_client_id,
    _strategy_role_from_client_id,
    _order_client_id,
    _BOT_CLOSE_FILL_TTL_SEC,
    _recent_bot_close_fills,
    _record_bot_close_fill,
    _prune_recent_bot_close_fills,
    _has_recent_bot_close_on_leg,
    _consume_recent_bot_close_fill,
    _strategy_from_algo_order,
    _utc_now_iso,
    _position_entry_age_sec,
    _is_recent_entry,
    _position_registry,
    _upsert_position_registry,
    _mark_position_registry_closed,
    _prune_position_registry,
    _strategy_from_position_registry,
)

from bot.engine.sync_helpers import (
    _dt_to_ms,
    _history_has_sync_close,
    record_exchange_sync_close,
)

print("[Startup] Loading modules complete — starting services…", flush=True)
load_state()

# Runtime
SCAN_SYMBOLS: list[str] = []
SCAN_TICKER_BY_SYM: dict[str, dict] = {}
latest_prices: dict[str, float] = {}
latest_marks: dict[str, float] = {}              # premiumIndex mark — paper SL/TP triggers (like exchange)
_last_mark_refresh_at: dict[str, float] = {}
_PAPER_MARK_REFRESH_INTERVAL_SEC = 2.0
_http_client: httpx.AsyncClient | None = None   # shared, initialised in lifespan
_telegram_client: httpx.AsyncClient | None = None  # dedicated short-timeout client
_eval_lock = asyncio.Lock()                      # prevents overlapping signal scans
_state_lock = asyncio.Lock()                     # serialises mutations to open_positions
_pending_entry_retries: list[dict] = []          # -4192 retry + price-wait entry queue
exchange_account: dict = {}                      # live Binance account snapshot (LIVE_MODE only)
_pionex_balance_snapshot: dict = {
    "configured": False,
    "ok": False,
    "total_in_usdt": None,
    "total_in_btc": None,
    "bot_account_usdt": None,
    "trader_account_usdt": None,
    "total_in_thb": None,
    "usdt_thb_rate": None,
    "error": None,
    "updated_at": None,
}
_symbol_leverage: dict[str, int] = {}            # per-symbol leverage from REST or set_leverage
_today_binance_profit: dict = {                  # UTC-day net from /fapi/v1/income (Binance "Today PnL")
    "net": 0.0,
    "realized_pnl": 0.0,
    "commission": 0.0,
    "funding": 0.0,
    "date_utc": "",
}
_last_income_today_sync_mono: float = 0.0        # throttle /fapi/v1/income today pull
_daily_profit_30d: list[dict] = []                # UTC-day net series for dashboard bar chart
_last_daily_profit_30d_sync_mono: float = 0.0
_last_live_close_mono: float = 0.0               # adaptive income_sync_loop interval
_BINANCE_CLOSE_HISTORY_CACHE: list[dict] = []
_BINANCE_RAW_TRADES_CACHE: list[dict] = []       # /api/trades + dashboard (from last history pull)
_BINANCE_TRADES_BY_SYMBOL: dict[str, list] = {}  # round-robin userTrades cache
_BINANCE_HISTORY_SYMBOL_INDEX: int = 0
_BINANCE_CLOSE_HISTORY_FETCHED_AT: float = 0.0
_BINANCE_CLOSE_HISTORY_LOCK = asyncio.Lock()
_last_uds_account_update_mono: float = 0.0       # monotonic — ACCOUNT_UPDATE freshness
_MARGIN_HISTORY_CAP = 35040                      # 1 year at 15-min interval (365×24×4) — persisted in state["margin_history"]
_MARGIN_SAMPLE_INTERVAL_SEC = 900                # 15 minutes between margin samples
_SYNC_ISSUES_CAP = 50                            # most-recent sync issues — persisted in state["sync_issues"]
_SYNC_ISSUE_DEDUP_SEC = 300.0                   # suppress duplicate sync-issue messages within 5 minutes
_SYNC_ISSUE_MAX_AGE_SEC = 86400.0               # auto-drop resolved sync issues after 24h
_PRICE_TICK_MONITOR_INTERVAL_HYBRID_SEC = 0.5   # faster local TP/SL checks when hybrid legs are open
_ERROR_EVENTS_CAP = 200                          # lightweight runtime monitor history
_ERROR_EVENT_DEDUP_SEC = 120.0                   # avoid repeating the same dashboard/Telegram event
_ALGO_CAPACITY_WARN_SEC = 300.0                  # avoid warning every candidate/sync tick when full
_SYNC_ENTRY_GRACE_SEC = 180.0                    # wait for Binance positionRisk to catch up after fresh entries
_ENTRY_STAGGER_ARMED = False
_PRICE_WS_READ_TIMEOUT_SEC = 90                  # fallback if combined stream is quiet (mark@1s normally ~1Hz)
_PRICE_WS_PING_INTERVAL_SEC = 20                 # detect half-open sockets (Binance server ping ~3m)
_PRICE_WS_PING_TIMEOUT_SEC = 60
_PRICE_TICK_MONITOR_INTERVAL_SEC = 1.0           # paper/local SL-TP checks decoupled from WS recv loop
_WS_SYMBOL_CACHE_SEC = 30.0                      # refresh SCAN_SYMBOLS + open symbol filter for all-ticker stream
_DASHBOARD_MARKET_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT", "XAUUSDT", "CLUSDT"})  # dashboard Live Prices ticker
_BOT_STARTED_AT = datetime.now(timezone.utc)
_ENTRY_GATE_LOGGED: set[str] = set()
_entry_busy_until_mono: float = 0.0           # defer userTrades PnL work during entry windows
_entry_busy_defer_log_at: float = 0.0
_last_algo_capacity_warning_at: float = 0.0
_dashboard_algo_order_count: int | None = None
_uds_connected: bool = False                     # User Data Stream connection status
_last_uds_connected_at: str | None = None
_last_uds_error_at: str | None = None
_last_sync_ok_at: str | None = None
_last_sync_error_at: str | None = None
_last_exchange_account_ok_at: str | None = None
_last_exchange_account_error_at: str | None = None
_last_price_ws_ok_at: str | None = None
_last_scheduler_ok_at: str | None = None
_last_watchdog_ok_at: str | None = None
_watchdog_last_alert_at: dict[str, float] = {}
_circuit_breaker: bool = False                   # True = daily loss limit hit, no new entries
_BINANCE_RATE_LIMIT_UNTIL_MS: int = 0            # epoch ms — skip REST-heavy paths while IP banned
_BINANCE_RATE_LIMIT_REASON: str = ""             # last ban / rate-limit detail for dashboard
_BINANCE_RATE_LIMIT_ALERT_SENT: bool = False     # one Telegram alert per ban window
_EMERGENCY_CLOSE_RETRY_TASK: asyncio.Task | None = None
_EMERGENCY_CLOSE_RETRY_PENDING: dict | None = None  # {"keys": [...], "full_leg": bool}
_daily_loss_usd: float = 0.0                     # accumulated realized loss today (resets at UTC midnight)
_daily_loss_date: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")  # UTC date of _daily_loss_usd
_sync_close_leg_cache: dict[tuple[str, str], dict] = {}

def _position_should_use_local_sl_tp(tab_name: str | None = None) -> bool:
    """True when global policy is fully local (both legs bot-managed)."""
    return _effective_sltp_mode() == "local"


def _hydrate_circuit_breaker_from_state():
    """Pull persisted circuit-breaker fields into module globals after load_state."""
    global _circuit_breaker, _daily_loss_usd, _daily_loss_date
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    saved_date = state.get("daily_loss_date") or today
    if saved_date != today:
        # New UTC day — wipe prior counter but keep the date in sync
        _circuit_breaker = False
        _daily_loss_usd = 0.0
        _daily_loss_date = today
        state["circuit_breaker"] = False
        state["daily_loss_usd"] = 0.0
        state["daily_loss_date"] = today
    else:
        _circuit_breaker = bool(state.get("circuit_breaker", False))
        _daily_loss_usd = float(state.get("daily_loss_usd", 0.0))
        _daily_loss_date = saved_date

def _persist_circuit_breaker():
    """Mirror globals into state dict (caller must invoke save_state)."""
    state["circuit_breaker"] = _circuit_breaker
    state["daily_loss_usd"] = _daily_loss_usd
    state["daily_loss_date"] = _daily_loss_date

_hydrate_circuit_breaker_from_state()

from bot.feeds.market import (
    _EXCLUDE_SYMBOLS,
    _SCAN_EXCLUDE_UNDERLYING_TYPES,
    _scan_exclude_symbols,
    _price_feed_get,
    fetch_scan_symbols,
    _check_entry_quality,
    _effective_kline_fetch_delay_sec,
    _mark_within_entry_protection,
    _pre_entry_mark_protection_guard,
    _resolve_live_entry_fill,
    _position_in_local_monitor_grace,
    _fetch_mark_price,
    _mark_or_last,
    _last_or_mark,
    _entry_reference_price,
    _fetch_entry_reference_price,
    _price_for_sltp,
    _fetch_sltp_trigger_price,
    _refresh_dashboard_market_prices,
    _refresh_open_position_marks,
    _paper_simulate_entry_fill,
)

from bot.engine.protection_prices import (
    _protection_prices_from_entry,
    _paper_available_margin,
    _http_error_text,
    _is_algo_limit_error,
    _is_binance_cooling_off_error,
    _is_immediate_trigger_error,
    _exchange_sl_crossed_mark,
    _exchange_tp_crossed_mark,
    _missing_exchange_protection_reason,
    _round_protection_price,
    _mark_ref_for_exchange_nudge,
    _clamp_exchange_nudge,
    _planned_protection_prices,
)

from bot.engine.history import (
    _sltp_diff_pct_from_entry,
    _position_sltp_snapshot_fields,
    _sltp_diff_fields_from_row,
    _position_sltp_diff_fields,
    _enrich_history_entry,
    _ms_to_iso,
    _estimate_entry_from_close,
    _bot_side_from_binance_trade,
    _symbols_for_trade_history,
    _fetch_symbol_user_trades,
    _binance_trade_to_api_row,
    _resolve_symbol_leverage,
    _ensure_symbol_leverage,
    _uds_position_row,
    _apply_uds_account_update,
    _aggregate_binance_close_rows,
    _merge_bot_metadata_into_binance_close,
    _rebuild_binance_history_caches,
    _next_trade_history_batch,
    _refresh_binance_close_history,
    binance_close_history_loop,
    _dashboard_binance_recent_history,
    _dashboard_recent_history_source,
    _dashboard_recent_history,
    _dashboard_stats_trade_history,
    _history_rows_as_api_trades,
    _dashboard_equity_close_series,
    _dashboard_equity_curve_api,
    _dashboard_strategy_stats_api,
    _paginated_history_page,
    _recovered_tab_summary,
    _dashboard_stats_meta,
)

def _validate_planned_protection(
    sym: str,
    side: str,
    qty: float,
    entry_px: float,
    sl_price: float,
    tp_price: float,
    mark_px: float | None,
    use_local_protection: bool = False,
) -> tuple[bool, str]:
    """Pre-entry validation for protection parameters before a market order is sent."""
    if qty <= 0:
        return False, f"{sym}: qty {qty} is not positive"
    if entry_px <= 0:
        return False, f"{sym}: entry price {entry_px} is not positive"
    if sl_price <= 0 or tp_price <= 0:
        return False, f"{sym}: SL/TP must be positive (SL={sl_price}, TP={tp_price})"
    if sl_price == tp_price:
        return False, f"{sym}: SL and TP are identical ({sl_price})"

    if use_local_protection:
        if side == "Long":
            if not (sl_price < entry_px < tp_price):
                return False, (
                    f"{sym}: Long local protection wrong side of entry {entry_px} "
                    f"(SL={sl_price}, TP={tp_price})"
                )
        else:
            if not (tp_price < entry_px < sl_price):
                return False, (
                    f"{sym}: Short local protection wrong side of entry {entry_px} "
                    f"(SL={sl_price}, TP={tp_price})"
                )
    else:
        ref, _ = _mark_ref_for_exchange_nudge(entry_px, mark_px)
        if side == "Long":
            if not (sl_price < ref < tp_price):
                return False, f"{sym}: Long protection wrong side of mark/ref {ref} (SL={sl_price}, TP={tp_price})"
        else:
            if not (tp_price < ref < sl_price):
                return False, f"{sym}: Short protection wrong side of mark/ref {ref} (SL={sl_price}, TP={tp_price})"

    if side == "Long":
        risk_pct = (entry_px - sl_price) / entry_px
    else:
        risk_pct = (sl_price - entry_px) / entry_px

    if risk_pct <= 0:
        return False, f"{sym}: non-positive protection risk {risk_pct:.4%}"
    # MAX_SL_PCT is inclusive; allow price-rounding slack before rejecting.
    _cap_slack = 1e-4
    if risk_pct >= MAX_SL_PCT + _cap_slack:
        return False, f"{sym}: protection risk {risk_pct:.2%} exceeds max {MAX_SL_PCT:.2%}"

    min_qty = float(binance_live.symbol_info.get(sym, {}).get("min_qty", 0) or 0)
    rounded_qty = binance_live.round_qty(sym, qty)
    if rounded_qty <= 0 or rounded_qty < min_qty:
        return False, f"{sym}: rounded qty {rounded_qty} below minimum {min_qty}"

    if not use_local_protection:
        # Live exchange protection must have planned prices.
        if not sl_price or not tp_price:
            return False, f"{sym}: missing exchange protection prices"

    return True, ""


async def _simulate_entry_protection(
    sym: str,
    sig: dict,
    qty: float,
    use_local_protection: bool,
    entry_ref_px: float | None = None,
) -> tuple[bool, str, float, float, float]:
    """Best-effort pre-flight of SL/TP shape before sending the market entry."""
    trigger_px = await _fetch_sltp_trigger_price(sym)
    if entry_ref_px and entry_ref_px > 0:
        entry_ref = float(entry_ref_px)
    else:
        entry_ref = trigger_px or float(sig["ep"])
    sl_price, tp_price = _planned_protection_prices(
        sym,
        sig["side"],
        entry_ref,
        trigger_px,
        float(sig["sl"]),
        float(sig["tp"]),
        float(sig["ep"]),
        use_local_protection=use_local_protection,
    )
    ok, reason = _validate_planned_protection(
        sym,
        sig["side"],
        qty,
        entry_ref,
        sl_price,
        tp_price,
        trigger_px,
        use_local_protection=use_local_protection,
    )
    return ok, reason, sl_price, tp_price, entry_ref


from bot.engine.pnl import (
    _position_mark_price,
    _position_price,
    _unrealized_pnl_usd,
    _refresh_exchange_account_marks,
    _recalculate_unrealized_pnls,
    _binance_income_net,
    _strategy_realized_total,
    _ensure_equity_curve_baselines,
    _dashboard_equity_curve_baseline,
    _append_equity_snapshot,
    _BINANCE_GROSS_TYPES,
    _GROSS_BREAKDOWN_VERSION,
    _INCOME_TAB_MATCH_MS,
    _INCOME_SYNC_POLL_SEC,
    _INCOME_SYNC_POLL_ACTIVE_SEC,
    _INCOME_SYNC_IDLE_AFTER_CLOSE_SEC,
    _INCOME_TODAY_REFRESH_SEC,
    _INCOME_DAILY_30D_REFRESH_SEC,
    _DAILY_PROFIT_30D_DAYS,
    _SEEN_TRAN_IDS_CAP,
    _empty_tab_income,
    _normalize_binance_income_state,
    _normalize_binance_tab_income,
    _income_seen_tran_set,
    _income_tran_already_applied,
    _mark_income_tran_applied,
    _persist_income_seen_tran,
    _accumulate_gross_breakdown,
    _binance_gross_net,
    _tab_gross_from_history,
    _effective_tab_gross,
    _history_entry_position_side,
    _attribute_income_to_tab,
    _apply_income_gross_record,
    _apply_income_record,
    _utc_day_start_ms,
    _utc_day_str_from_ms,
    _daily_profit_30d_date_keys,
    _group_income_by_utc_day,
    _build_daily_profit_30d_series,
    _summarize_income_records,
    _sync_today_income_once,
    _sync_daily_profit_30d_once,
    _maybe_sync_daily_profit_30d,
    _bootstrap_daily_profit_30d,
    _dashboard_daily_profit_30d,
    _dashboard_today_profit,
    _exchange_unrealized_total,
    _dashboard_exchange_account,
    _dashboard_pionex_balance,
    _dashboard_pnl_summary,
)

async def _check_local_position_exits_unsafe():
    for pos_key, pos in list(state["open_positions"].items()):
        sym = pos.get("symbol") or pos_key.split("_", 1)[0]
        if LIVE_MODE and _position_in_local_monitor_grace(pos):
            continue
        price = _position_price(pos)
        if price is None:
            continue

        if pos["side"] == "Long":
            local_reason = _local_protection_reason(pos, price) if LIVE_MODE else None
            missing_exchange_reason = _missing_exchange_protection_reason(pos, price) if LIVE_MODE else None
            if not LIVE_MODE and price <= pos["sl"]:
                await _close_position_unsafe(pos_key, float(pos["sl"]), "SL")
            elif local_reason == "SL":
                print(
                    f"[Local SL] {_utc_log_stamp()}  {pos_key}  price={_log_price(sym, price)}"
                    f"  <= SL={_log_price(sym, pos['sl'])}"
                )
                await _close_position_unsafe(pos_key, float(pos["sl"]), "SL")
            elif missing_exchange_reason == "SL":
                print(
                    f"[Missing Exchange SL] {_utc_log_stamp()}  {pos_key}  price={_log_price(sym, price)}"
                    f"  crossed SL={_log_price(sym, pos['sl'])}"
                )
                await _close_position_unsafe(pos_key, float(pos["sl"]), "SL")
            elif not LIVE_MODE and price >= pos["tp"]:
                await _close_position_unsafe(pos_key, float(pos["tp"]), "TP")
            elif local_reason == "TP":
                print(
                    f"[Local TP] {_utc_log_stamp()}  {pos_key}  price={_log_price(sym, price)}"
                    f"  >= TP={_log_price(sym, pos['tp'])}"
                )
                await _close_position_unsafe(pos_key, float(pos["tp"]), "TP")
            elif missing_exchange_reason == "TP":
                print(
                    f"[Missing Exchange TP] {_utc_log_stamp()}  {pos_key}  price={_log_price(sym, price)}"
                    f"  crossed TP={_log_price(sym, pos['tp'])}"
                )
                await _close_position_unsafe(pos_key, float(pos["tp"]), "TP")
        else:
            local_reason = _local_protection_reason(pos, price) if LIVE_MODE else None
            missing_exchange_reason = _missing_exchange_protection_reason(pos, price) if LIVE_MODE else None
            if not LIVE_MODE and price >= pos["sl"]:
                await _close_position_unsafe(pos_key, float(pos["sl"]), "SL")
            elif local_reason == "SL":
                print(
                    f"[Local SL] {_utc_log_stamp()}  {pos_key}  price={_log_price(sym, price)}"
                    f"  >= SL={_log_price(sym, pos['sl'])}"
                )
                await _close_position_unsafe(pos_key, float(pos["sl"]), "SL")
            elif missing_exchange_reason == "SL":
                print(
                    f"[Missing Exchange SL] {_utc_log_stamp()}  {pos_key}  price={_log_price(sym, price)}"
                    f"  crossed SL={_log_price(sym, pos['sl'])}"
                )
                await _close_position_unsafe(pos_key, float(pos["sl"]), "SL")
            elif not LIVE_MODE and price <= pos["tp"]:
                await _close_position_unsafe(pos_key, float(pos["tp"]), "TP")
            elif local_reason == "TP":
                print(
                    f"[Local TP] {_utc_log_stamp()}  {pos_key}  price={_log_price(sym, price)}"
                    f"  ≤ TP={_log_price(sym, pos['tp'])}"
                )
                await _close_position_unsafe(pos_key, float(pos["tp"]), "TP")
            elif missing_exchange_reason == "TP":
                print(
                    f"[Missing Exchange TP] {_utc_log_stamp()}  {pos_key}  price={_log_price(sym, price)}"
                    f"  crossed TP={_log_price(sym, pos['tp'])}"
                )
                await _close_position_unsafe(pos_key, float(pos["tp"]), "TP")


def _local_protection_reason(pos: dict, price: float | None) -> str | None:
    if price is None:
        return None
    sl_px = float(pos.get("sl", 0) or 0)
    tp_px = float(pos.get("tp", 0) or 0)
    if sl_px <= 0 or tp_px <= 0:
        return None
    if pos.get("side") == "Long":
        if _position_sl_is_local(pos) and price <= sl_px:
            return "SL"
        if _position_tp_is_local(pos) and price >= tp_px:
            return "TP"
    else:
        if _position_sl_is_local(pos) and price >= sl_px:
            return "SL"
        if _position_tp_is_local(pos) and price <= tp_px:
            return "TP"
    return None


async def _open_algo_order_count() -> int | None:
    try:
        data = await binance_live._sreq(_http_client, "GET", "/fapi/v1/openAlgoOrders", {})
        orders = _active_algo_orders(data)
        return len(orders)
    except Exception as e:
        print(f"[Live] Could not check algo order capacity: {e}")
        return None


async def _has_algo_capacity(required_slots: int, context: str) -> bool:
    """Return whether Binance has enough free algo-order slots for protective legs."""
    global _last_algo_capacity_warning_at
    count = await _open_algo_order_count()
    if count is None:
        return True
    free = _max_open_algo_orders() - count
    if free >= required_slots:
        return True

    msg = (
        f"Skip {context}: open algo order limit {count}/{_max_open_algo_orders()}, "
        f"need {required_slots} free slot(s)"
    )
    print(f"[Live] {msg}")
    import time as _time_mod
    now = _time_mod.time()
    if now - _last_algo_capacity_warning_at >= _ALGO_CAPACITY_WARN_SEC:
        _last_algo_capacity_warning_at = now
        await record_error_event(msg, severity="warning", source="algo_capacity", notify=False)
    return False


async def _live_position_qty(sym: str, pos_side: str) -> float:
    """Fresh Binance position amount for one hedge-mode side."""
    risk = await binance_live.get_position_risk(_http_client)
    for p in risk:
        if p.get("symbol") != sym:
            continue
        if _position_tuple_from_exchange(p)[1] != str(pos_side).upper():
            continue
        return abs(float(p.get("positionAmt", 0) or 0))
    return 0.0


def _protective_order_matches(
    order: dict,
    sym: str,
    pos_side: str,
    order_type: str,
    qty: float,
    trigger_px: float,
) -> bool:
    if order.get("symbol") != sym:
        return False
    if str(order.get("positionSide") or "").upper() != str(pos_side).upper():
        return False
    if _algo_order_type(order) != order_type:
        return False
    order_qty = _algo_quantity(order)
    order_trigger = _algo_trigger_price(order)
    qty_tol = max(qty * 0.002, 1e-9)
    px_tol = max(abs(trigger_px) * 0.0005, 1e-12)
    return abs(order_qty - qty) <= qty_tol and abs(order_trigger - trigger_px) <= px_tol


async def _find_existing_protective_order(
    sym: str,
    pos_side: str,
    order_type: str,
    qty: float,
    trigger_px: float,
    cached_orders: list[dict] | None = None,
) -> dict | None:
    """Find a matching open SL/TP before placing a new one.

    Binance testnet can briefly return stale all-symbol algo-order snapshots.
    Prefer a symbol-scoped fetch when adopting a missing leg because it is more
    precise and avoids creating duplicate protective orders during repair.
    """
    candidates = []
    for order in cached_orders or []:
        if _protective_order_matches(order, sym, pos_side, order_type, qty, trigger_px):
            candidates.append(order)
    try:
        data = await binance_live._sreq(_http_client, "GET", "/fapi/v1/openAlgoOrders", {"symbol": sym})
        symbol_orders = _active_algo_orders(data)
        for order in symbol_orders:
            if _protective_order_matches(order, sym, pos_side, order_type, qty, trigger_px):
                candidates.append(order)
    except Exception as e:
        print(f"[Live Sync] Could not fetch symbol algo orders for {sym}: {e}")

    if not candidates:
        return None

    # Newer repairs often have higher algoIds, but any matching leg protects
    # the same aggregate hedge position. Choose deterministically.
    candidates.sort(key=lambda o: int(o.get("algoId", 0) or 0), reverse=True)
    return candidates[0]


async def _strategy_from_recent_entry_order(
    sym: str,
    pos_side: str,
    side: str,
    entry_px: float,
    qty: float,
) -> tuple[str | None, str | None]:
    """Best-effort owner recovery from recent Binance entry clientOrderId."""
    try:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        orders = await binance_live._sreq(_http_client, "GET", "/fapi/v1/allOrders", {
            "symbol": sym,
            "startTime": now_ms - (7 * 24 * 60 * 60 * 1000),
            "limit": 1000,
        })
    except Exception as e:
        print(f"[Live Sync] Could not fetch recent orders for {sym}: {e}")
        return None, None

    expected_side = "BUY" if side == "Long" else "SELL"
    candidates = []
    for o in orders if isinstance(orders, list) else []:
        client_id = _order_client_id(o)
        tab = _strategy_from_client_id(client_id)
        if not tab:
            continue
        role = _strategy_role_from_client_id(client_id)
        if role and role != "ENTRY":
            continue
        if str(o.get("positionSide") or "").upper() != str(pos_side).upper():
            continue
        if str(o.get("side") or "").upper() != expected_side:
            continue
        if str(o.get("status") or "").upper() not in {"FILLED", "PARTIALLY_FILLED"}:
            continue
        try:
            order_qty = float(o.get("executedQty") or o.get("origQty") or 0)
            avg_px = float(o.get("avgPrice") or 0)
            order_ts = int(o.get("updateTime") or o.get("time") or 0)
        except Exception:
            continue
        if order_qty <= 0:
            continue

        qty_score = abs(order_qty - qty) / max(qty, order_qty, 1e-12)
        if qty_score > 0.35:
            continue
        px_score = abs(avg_px - entry_px) / max(entry_px, avg_px, 1e-12) if avg_px > 0 and entry_px > 0 else 0.0
        if px_score > 0.03:
            continue
        age_score = max(0.0, (now_ms - order_ts) / (7 * 24 * 60 * 60 * 1000)) if order_ts else 1.0
        role_bonus = 0.0 if role == "ENTRY" else 0.25
        candidates.append((qty_score + px_score + age_score + role_bonus, order_ts, tab, client_id))

    if not candidates:
        return None, None

    candidates.sort(key=lambda item: (item[0], -item[1]))
    _, _, tab, client_id = candidates[0]
    return tab, f"entry order {client_id}"


async def _verify_live_position_protection(pos_key: str) -> tuple[bool, str]:
    pos = state.get("open_positions", {}).get(pos_key)
    if not LIVE_MODE or not pos:
        return True, "not live or position missing"

    sym = pos.get("symbol")
    pos_side = _position_side_from_state(pos)
    expected_qty = float(pos.get("qty", 0) or 0)
    try:
        live_qty = await _live_position_qty(sym, pos_side)
    except Exception as e:
        _note_binance_rate_limit(e)
        return False, f"{pos_key}: could not verify live qty after entry: {e}"
    if live_qty <= 0:
        return False, f"{pos_key}: live position not found after entry"
    if expected_qty > 0 and live_qty + max(expected_qty * 0.001, 1e-9) < expected_qty:
        return False, f"{pos_key}: live qty {live_qty} below state qty {expected_qty}"

    if pos.get("protection_mode") == "local" or _position_full_local(pos):
        return True, f"{pos_key}: live position verified with bot-managed local SL/TP"
    if str(pos.get("protection_mode") or "").lower() == "hybrid" or (
        _position_tp_is_local(pos) != _position_sl_is_local(pos)
    ):
        sl_oid = pos.get("sl_order_id")
        if _position_sl_is_local(pos):
            return True, f"{pos_key}: hybrid local SL verified"
        if not sl_oid:
            return False, f"{pos_key}: missing tracked SL after hybrid entry"
        try:
            data = await binance_live._sreq(_http_client, "GET", "/fapi/v1/openAlgoOrders", {"symbol": sym})
            orders = _active_algo_orders(data)
        except Exception as e:
            return False, f"{pos_key}: could not verify hybrid SL order: {e}"
        alive_ids = {
            int(o.get("algoId", 0) or 0)
            for o in orders
            if str(o.get("positionSide") or "").upper() == pos_side
        }
        if int(sl_oid) not in alive_ids:
            return False, f"{pos_key}: missing live SL after hybrid entry"
        return True, f"{pos_key}: hybrid SL on exchange, TP bot-managed"

    sl_oid = pos.get("sl_order_id")
    tp_oid = pos.get("tp_order_id")
    if not sl_oid or not tp_oid:
        return False, f"{pos_key}: missing tracked SL/TP ids after entry"

    try:
        data = await binance_live._sreq(_http_client, "GET", "/fapi/v1/openAlgoOrders", {"symbol": sym})
        orders = _active_algo_orders(data)
    except Exception as e:
        return False, f"{pos_key}: could not verify open SL/TP orders: {e}"

    alive_ids = {
        int(o.get("algoId", 0) or 0)
        for o in orders
        if str(o.get("positionSide") or "").upper() == pos_side
    }
    missing = []
    if int(sl_oid) not in alive_ids:
        missing.append("SL")
    if int(tp_oid) not in alive_ids:
        missing.append("TP")
    if missing:
        return False, f"{pos_key}: missing live {'/'.join(missing)} after entry"

    return True, f"{pos_key}: live entry and Binance SL/TP verified"


async def _cleanup_failed_live_entry(
    pos_key: str,
    sym: str,
    pos_side: str,
    close_side: str,
    qty: float,
    tab_name: str,
) -> None:
    """Drop phantom state or rollback a partial live entry after an exception."""
    pos = state["open_positions"].get(pos_key)
    if not pos:
        return
    rollback_qty = float(qty or pos.get("qty") or 0)
    live_qty, live_err = await _resolve_live_qty(sym, pos_side)
    if live_err is not None and live_qty is None:
        print(f"[Live] entry cleanup deferred for {pos_key}: {live_err}")
        return
    if (live_qty or 0.0) <= 1e-9:
        state["open_positions"].pop(pos_key, None)
        _mark_position_registry_closed(pos_key, "entry_cleanup_no_live_qty")
        await save_state()
        print(f"[Live] Removed stale entry state {pos_key} (exchange flat after entry error)")
        return
    if rollback_qty <= 0:
        return
    try:
        await binance_live.place_market_order(
            _http_client,
            sym,
            close_side,
            rollback_qty,
            position_side=pos_side,
            client_order_id=_strategy_client_id(tab_name, pos_side, "CLOSE"),
        )
        state["open_positions"].pop(pos_key, None)
        _mark_position_registry_closed(pos_key, "entry_rollback")
        await save_state()
        print(f"[Live] Rolled back failed entry {pos_key}")
    except Exception as rb_err:
        _note_binance_rate_limit(rb_err)
        print(f"[Live] entry rollback failed {pos_key}: {rb_err} — position may remain on exchange")


async def _safeguard_prices_for_recovery(sym: str, side: str, entry_px: float) -> tuple[float, float]:
    sl_px = tp_px = 0.0
    try:
        klines = await get_klines(sym, "1h", limit=50)
        if klines:
            df_sg = pd.DataFrame(klines, columns=["timestamp", "open", "high", "low", "close", "volume"])
            atr_series = strategies._calc_atr(df_sg, 14).dropna()
            atr = float(atr_series.iloc[-1]) if len(atr_series) > 0 else 0.0
            if atr > 0 and not (atr != atr):
                if side == "Long":
                    sl_px = entry_px - (2.1 * atr)
                    tp_px = entry_px + (3.5 * atr)
                else:
                    sl_px = entry_px + (2.1 * atr)
                    tp_px = entry_px - (3.5 * atr)
    except Exception as atr_err:
        print(f"[Live Sync] ATR fetch failed for {sym}: {atr_err}")

    if sl_px <= 0 or tp_px <= 0:
        if side == "Long":
            sl_px = entry_px * 0.975
            tp_px = entry_px * 1.050
        else:
            sl_px = entry_px * 1.025
            tp_px = entry_px * 0.950

    return binance_live.round_price(sym, sl_px), binance_live.round_price(sym, tp_px)


async def _recover_untracked_live_qty(
    sym: str,
    pos_side: str,
    side: str,
    entry_px: float,
    qty: float,
    recovery_source: str,
) -> str | None:
    """Create a recovered state leg for live exchange qty not covered by bot state."""
    if qty <= 0:
        return None
    recovered_tab = "SafeGuard"
    sl_px, tp_px = await _safeguard_prices_for_recovery(sym, side, entry_px)
    close_side = "SELL" if side == "Long" else "BUY"
    sl_oid = tp_oid = None
    sl_client_id = _strategy_client_id(recovered_tab, pos_side, "SL")
    tp_client_id = _strategy_client_id(recovered_tab, pos_side, "TP")
    prot_plan = _resolve_entry_protection_plan(_effective_sltp_mode(), await _open_algo_order_count())
    if prot_plan is None:
        await _has_algo_capacity(2, f"{sym} {pos_side} excess qty recovery")
        return None
    sl_local, tp_local, prot_reason = prot_plan
    protection_mode = None
    protection_reason = None
    protection_status = "exchange"

    if sl_local and tp_local:
        sl_client_id = None
        tp_client_id = None
        await record_sync_issue(
            f"{sym} {pos_side} excess qty {qty} recovered with bot-managed local SL/TP ({prot_reason})"
        )
    elif not sl_local and not tp_local:
        capacity_count = await _open_algo_order_count()
        if capacity_count is not None and (_max_open_algo_orders() - capacity_count) < 2:
            await _has_algo_capacity(2, f"{sym} {pos_side} excess qty recovery")
            return None
    else:
        sl_client_id = _strategy_client_id(recovered_tab, pos_side, "SL") if not sl_local else None
        tp_client_id = _strategy_client_id(recovered_tab, pos_side, "TP") if not tp_local else None

    if not sl_local or not tp_local:
        try:
            if not sl_local:
                sl_res = await binance_live.place_stop_loss(
                    _http_client, sym, close_side, sl_px, qty, position_side=pos_side,
                    client_algo_id=sl_client_id,
                )
                sl_oid = _algo_id(sl_res)
            if not tp_local:
                tp_res = await binance_live.place_take_profit(
                    _http_client, sym, close_side, tp_px, qty, position_side=pos_side,
                    client_algo_id=tp_client_id,
                )
                tp_oid = _algo_id(tp_res)
        except Exception as e:
            body = _http_error_text(e)
            print(f"[Live Sync] Failed to protect excess qty for {sym} {pos_side}: {e} | Binance: {body}")
            await record_error_event(
                f"Failed to protect excess live qty for {sym} {pos_side}: {body or e}",
                severity="critical",
                source="sync_excess_qty",
                notify=True,
            )
            return None

    base_key = f"{sym}_{pos_side}_Recovered"
    pos_key = base_key
    suffix = 2
    while pos_key in state["open_positions"]:
        pos_key = f"{base_key}_{suffix}"
        suffix += 1

    pos = {
        "tab": recovered_tab,
        "symbol": sym,
        "side": side,
        "position_side": pos_side,
        "entry_price": entry_px,
        "sl": sl_px,
        "tp": tp_px,
        "qty": qty,
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "sl_order_id": sl_oid,
        "tp_order_id": tp_oid,
        "sl_client_algo_id": sl_client_id,
        "tp_client_algo_id": tp_client_id,
        "recovery_source": recovery_source,
    }
    _apply_protection_sources(pos, sl_local=sl_local, tp_local=tp_local, reason=prot_reason)

    state["open_positions"][pos_key] = pos
    reg_status = "local" if _position_full_local(pos) else ("hybrid" if str(pos.get("protection_mode")) == "hybrid" else "open")
    _upsert_position_registry(pos_key, pos, status=reg_status)
    await _alert_position_protection_risk(
        pos_key,
        pos,
        source="sync_excess_qty",
        notify=True,
        sync_issue=_position_full_local(pos),
    )
    await record_sync_issue(
        f"Recovered excess live qty {sym} {side}/{pos_side} qty={qty} as {recovered_tab} via {recovery_source}"
    )
    print(f"[Live Sync] Recovered excess live qty as {pos_key}: qty={qty} SL={sl_px} TP={tp_px}")
    return pos_key


def _qty_match_tolerance(sym: str, qty: float) -> float:
    min_qty = float(binance_live.symbol_info.get(sym, {}).get("min_qty", 0) or 0)
    return max(abs(qty) * 1e-6, min_qty * 0.5, 1e-9)


def _exact_qty_match_candidates(
    candidates: list[tuple[str, dict]],
    fill_qty: float,
    sym: str,
) -> list[tuple[str, dict]]:
    if fill_qty <= 0:
        return []
    tol = _qty_match_tolerance(sym, fill_qty)
    return [
        (pk, p) for pk, p in candidates
        if abs(float(p.get("qty", 0) or 0) - fill_qty) <= tol
    ]


def _health_stale_thresholds() -> tuple[float, float]:
    """Return (account_max_age_sec, sync_max_age_sec) for health/watchdog stale checks."""
    if not LIVE_MODE:
        return 120.0, 180.0
    account_max = float(
        EXCHANGE_ACCOUNT_POLL_SEC_UDS if _uds_connected else EXCHANGE_ACCOUNT_POLL_SEC_NO_UDS
    ) * 1.5
    if _uds_connected:
        import time as _time_mod
        uds_fresh = (_time_mod.monotonic() - _last_uds_account_update_mono) <= float(UDS_ACCOUNT_FRESH_SEC)
        if uds_fresh:
            sync_base = float(max(EXCHANGE_ACCOUNT_SYNC_SEC_UDS, EXCHANGE_ACCOUNT_SYNC_SEC_UDS_CONNECTED))
        else:
            sync_base = float(EXCHANGE_ACCOUNT_SYNC_SEC_NO_UDS)
    else:
        sync_base = float(EXCHANGE_ACCOUNT_SYNC_SEC_NO_UDS)
    return account_max, sync_base * 1.2


def _has_hybrid_local_exit_positions() -> bool:
    for pos in state.get("open_positions", {}).values():
        if _position_sl_is_local(pos) != _position_tp_is_local(pos):
            return True
    return False


def _price_tick_monitor_interval_sec() -> float:
    if LIVE_MODE and _has_hybrid_local_exit_positions():
        return _PRICE_TICK_MONITOR_INTERVAL_HYBRID_SEC
    return _PRICE_TICK_MONITOR_INTERVAL_SEC


def _prune_sync_issues(stale_removed_keys: list[str] | None = None) -> bool:
    """Drop expired issues and ambiguous-manual-close notes resolved by stale removal."""
    issues = state.get("sync_issues", [])
    if not issues:
        return False
    now = datetime.now(timezone.utc)
    symbols_cleaned = set()
    if stale_removed_keys:
        for pk in stale_removed_keys:
            sym = str(pk).split("_", 1)[0]
            if sym:
                symbols_cleaned.add(sym)
    kept: list[dict] = []
    for issue in issues:
        msg = str(issue.get("message") or "")
        try:
            created = datetime.fromisoformat(str(issue.get("created_at", "")).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if (now - created).total_seconds() > _SYNC_ISSUE_MAX_AGE_SEC:
                continue
        except Exception:
            pass
        if symbols_cleaned and "Ambiguous manual close fill on" in msg:
            if any(f"on {sym} (" in msg for sym in symbols_cleaned):
                continue
        kept.append(issue)
    if len(kept) == len(issues):
        return False
    state["sync_issues"] = kept
    return True


async def record_sync_issue(message: str):
    """Append a live-sync anomaly that the operator should review on the dashboard."""
    issues = state.setdefault("sync_issues", [])
    now = datetime.now(timezone.utc)
    for existing in reversed(issues[-10:]):
        if existing.get("message") != message:
            continue
        try:
            created = datetime.fromisoformat(str(existing.get("created_at", "")).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if (now - created).total_seconds() < _SYNC_ISSUE_DEDUP_SEC:
                print(f"[SyncIssue] duplicate suppressed: {message}")
                return
        except Exception:
            pass
    issues.append({
        "message": message,
        "created_at": _utc_now_iso(),
    })
    if len(issues) > _SYNC_ISSUES_CAP:
        del issues[:len(issues) - _SYNC_ISSUES_CAP]
    try:
        await save_state()
    except Exception:
        pass
    try:
        await record_error_event(message, severity="warning", source="sync", notify=False)
    except Exception:
        pass
    print(f"[SyncIssue] {message}")


async def _alert_position_protection_risk(
    pos_key: str,
    pos: dict,
    source: str,
    notify: bool = True,
    sync_issue: bool = False,
):
    risk = _position_protection_risk(pos_key, pos)
    if not risk:
        return
    severity = "critical" if risk["level"] == "critical" else "warning"
    await record_error_event(
        risk["message"],
        severity=severity,
        source=source,
        notify=notify,
    )
    if sync_issue:
        await record_sync_issue(risk["message"])


async def refresh_scan_symbols_loop():
    """Re-fetch top symbols every 6 hours so delisted / low-volume symbols roll off."""
    while True:
        await asyncio.sleep(6 * 3600)
        await fetch_scan_symbols()


from bot.engine.exit import (
    _close_side_for_position,
    _consumed_close_trade_ids,
    _resolve_close_order_id,
    _match_close_trades,
    _STABLE_COMMISSION_ASSETS,
    _commission_asset_usd_rate,
    _trade_commission_parts,
    _sum_trades_commission_parts,
    _fill_net_pnl,
    _order_commission_parts,
    _order_fill_commission_usd,
    _fetch_commission_income_usd,
    _summarize_close_trades,
    _scale_close_summary,
    _history_qty_for_entry,
    _find_history_entry,
    _fetch_close_pnl_from_trades,
    _apply_history_pnl_update,
    _history_close_snapshot,
    _reconcile_pnl_from_binance,
    _finalize_exit_notify,
    _registry_row_for_history,
    _repair_history_sltp_diff_once,
    _repair_history_pnl_once,
    close_position,
    _is_percent_price_reject,
    _utc_now_ms,
    _is_binance_rate_limit_text,
    _is_binance_rate_limit,
    _binance_rate_limit_snapshot,
    _schedule_binance_ban_alert,
    _activate_binance_rate_limit,
    _note_binance_rate_limit,
    _binance_rate_limited,
    _fetch_live_qty_cache,
    _cached_live_qty,
    _resolve_live_qty,
    _close_position_unsafe,
)

from bot.feeds.klines import get_klines

from bot.engine.prescreen import (
    maybe_run_kline_prescreen,
    run_prescreen_for_interval,
)

from bot.engine.entry import (
    check_invalidations_loop,
    _tab_on,
    _tab_max_positions,
    _tab_max_side_positions,
    _symbol_min_required_notional,
    _entry_size_allowed,
    _tab_side_counts,
    _balanced_candidate_allowed,
    _side_cap_candidate_allowed,
    _entry_long_short_balance_allowed,
    _trade_side_allowed,
    _queue_entry_retries,
    _halt_entry_batch_on_cooling_off,
    _queue_price_wait_entry,
    _defer_entry_until_better_price,
    _setup_signal_ts_ms,
    _signal_candle_close_ms,
    _entry_retry_signal_expired,
    _entry_price_wait_expired,
    _entry_price_at_or_better,
    _entry_price_favorability_pct,
    _retry_item_price_ready,
    _retry_item_favorability,
    _partition_due_entry_retries,
    _requeue_deferred_entry_retries,
    _wait_for_entry_price_at_or_better,
    _retry_one_queued_entry,
    _process_due_entry_retries,
    entry_retry_loop,
    _open_balanced_candidates,
    _interval_hours,
    _interval_candle_closed_at,
    _next_interval_boundary_after,
    _scan_gate_opens_at,
    _entry_gate_opens_at,
    _scan_gate_open,
    _entry_gate_open,
    _entry_gate_status,
    _seconds_until_next_candle_eval,
    _candle_scan_at,
    _next_scheduler_wake_at,
    _reset_entry_stagger,
    _stagger_before_next_entry,
    _mark_entry_busy,
    _begin_entry_window,
    _release_entry_busy_after_eval,
    _entry_window_active,
    _await_entry_window_clear,
    _build_tab17_momentum_universe,
    scan_candle_signals,
    execute_scanned_entries,
    evaluate_candle_signals,
    execute_entry,
    _execute_entry_unsafe,
    _pending_entries_dict,
    _tab_pending_count,
    _tab_open_slot_count,
    _tab_slots_remaining,
    _tab_slot_counts_by_tab,
    _trim_pending_entries_for_tab,
    complete_limit_entry_fill,
    handle_entry_limit_order_update,
    reconcile_pending_entry_orders,
    entry_limit_ttl_loop,
)

from bot.feeds.ws import (
    _ws_price_symbol_filter,
    _apply_mark_price_array,
    _is_ws_last_price_row,
    _dispatch_binance_price_ws_payload,
    _process_binance_price_ws_message,
    _apply_ticker_array_prices,
    price_tick_monitor_loop,
    binance_ws_loop,
    price_poll_loop,
)

from bot.engine.sync import (
    handle_order_update,
    _fetch_exchange_account_rest,
    _uds_account_fresh,
    user_data_stream_loop,
    _sweep_algo_orders_for,
    purge_orphaned_algo_orders,
    sync_live_positions,
)

from bot.scheduler.loops import (
    scheduler_loop,
    exchange_account_loop,
    pionex_balance_loop,
    process_watchdog_loop,
    _rebuild_binance_gross_breakdown,
    _sync_income_once,
    trigger_income_sync,
    server_time_sync_loop,
    income_sync_loop,
)

from bot.api.web import (
    lifespan,
    app,
    no_cache_dashboard_assets,
    _check_auth,
    _require_testnet,
    _dashboard_market_prices,
    _dashboard_margin_history,
    get_dashboard,
    _dashboard_live_prices,
    _lite_core_price_symbols,
    _build_dashboard_ws_payload,
    _DASHBOARD_LITE_SLEEP_SEC,
    _DASHBOARD_LITE_FULL_EVERY,
    dashboard_ws,
    api_trades,
    api_scan,
    api_tab_enabled,
    api_tabs_enabled_all,
    api_long_short_balance,
    api_trade_side_mode,
    api_symbol_filter,
    api_symbol_allowlist,
    api_symbol_blocklist,
    api_sltp_mode,
    api_local_sltp,
    api_max_positions,
    api_notional_size,
    api_leverage,
    api_margin_size,
    api_symbol_scan_limit,
    api_clear_sync_issues,
    api_clear_health_warnings,
    _group_position_keys_by_leg,
    _emergency_close_retry_worker,
    _schedule_emergency_close_retry,
    _finalize_closed_positions,
    _emergency_close_batch,
    _open_position_keys_for_side,
    _dashboard_emergency_close,
    api_close_all,
    api_close_all_long,
    api_close_all_short,
    api_close_strategy,
    api_test_hedge,
    api_test_hedge_same_side,
    api_test_full_hedge,
    api_logs,
    api_health,
    _INSTANCE_LOCK_HANDLE,
    _INSTANCE_MUTEX_HANDLE,
    _INSTANCE_MUTEX_ALREADY_EXISTS,
    _INSTANCE_MUTEX_NAME,
    app,
)

def _acquire_single_instance_lock() -> bool:
    """Hold an OS-level lock so only one bot instance can run at a time."""
    global _INSTANCE_LOCK_HANDLE, _INSTANCE_MUTEX_HANDLE
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.lock")

    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateMutexW(None, False, _INSTANCE_MUTEX_NAME)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        if ctypes.get_last_error() == _INSTANCE_MUTEX_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        _INSTANCE_MUTEX_HANDLE = handle

    try:
        fh = open(lock_path, "a+", encoding="utf-8")
    except Exception:
        if _INSTANCE_MUTEX_HANDLE and os.name == "nt":
            import ctypes
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(_INSTANCE_MUTEX_HANDLE)
            _INSTANCE_MUTEX_HANDLE = None
        raise
    try:
        fh.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                fh.close()
                if _INSTANCE_MUTEX_HANDLE:
                    import ctypes
                    ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(_INSTANCE_MUTEX_HANDLE)
                    _INSTANCE_MUTEX_HANDLE = None
                return False
        else:
            import fcntl
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                return False
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n{datetime.now(timezone.utc).isoformat()}\n")
        fh.flush()
        _INSTANCE_LOCK_HANDLE = fh
        return True
    except Exception:
        fh.close()
        if _INSTANCE_MUTEX_HANDLE and os.name == "nt":
            import ctypes
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(_INSTANCE_MUTEX_HANDLE)
            _INSTANCE_MUTEX_HANDLE = None
        raise


def _release_single_instance_lock() -> None:
    global _INSTANCE_LOCK_HANDLE, _INSTANCE_MUTEX_HANDLE
    if _INSTANCE_LOCK_HANDLE:
        try:
            _INSTANCE_LOCK_HANDLE.close()
        except Exception:
            pass
        _INSTANCE_LOCK_HANDLE = None
    if _INSTANCE_MUTEX_HANDLE and os.name == "nt":
        try:
            import ctypes
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(_INSTANCE_MUTEX_HANDLE)
        except Exception:
            pass
        _INSTANCE_MUTEX_HANDLE = None


atexit.register(_release_single_instance_lock)



_logmod.register_log_hooks(send_telegram=send_telegram, record_error_event=record_error_event)
