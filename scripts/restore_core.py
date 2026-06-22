"""Rebuild bot/core.py from git server.py + extracted modules (behavior-preserving)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "bot" / "core.py"


def _read_git_server() -> str:
    import subprocess

    proc = subprocess.run(
        ["git", "show", "HEAD:server.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return proc.stdout

HEADER = '''"""Trading engine core — extracted from server.py (behavior-preserving)."""
from __future__ import annotations

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
import strategies
import binance_live
import pionex_live
from bot.engine.signals_registry import TAB_EVALUATORS_1H, TAB_EVALUATORS_4H, evaluate_tab_signal
from bot.state.schema import default_state, default_state_corrupt_recovery
from config import (
    INITIAL_BALANCE, NOTIONAL_SIZE,
    ENTRY_FEE_PCT, EXIT_FEE_MAKER_PCT, EXIT_FEE_TAKER_PCT, SLIPPAGE_PCT,
    MAX_POSITIONS_PER_TAB, SYMBOL_SCAN_LIMIT, KLINE_FETCH_CONCURRENCY, STARTUP_ENABLED_TABS,
    HISTORY_CAP, USED_SETUPS_CAP, MAX_SL_PCT,
    BINANCE_CLOSE_HISTORY_ENABLED,
    BINANCE_CLOSE_HISTORY_DAYS, BINANCE_CLOSE_HISTORY_SYMBOL_CAP,
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
    BINANCE_IP_BAN_DEFAULT_BACKOFF_SEC,
    TAB_TIMEFRAMES, TABS,
    LIVE_MODE, LEVERAGE, BINANCE_TESTNET, LOCAL_SLTP, SLTP_MODE, SLTP_MODES, BINANCE_API_ACCOUNT_TYPE, LOW_MARGIN_THRESHOLD, MIN_ENTRY_AVAILABLE_MARGIN,
    ORDER_ENV, PRICE_FEED_ENV, PRICE_FEED_BASE_URL, PRICE_FEED_WS_URL,
    DASHBOARD_PASSCODE, DASHBOARD_AUTH_ENABLED, DASHBOARD_ALLOWED_ORIGINS, EQUITY_CURVE_MARGIN_BASELINE,
    CIRCUIT_BREAKER_DAILY_LOSS,
    TELEGRAM_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    PIONEX_CONFIGURED, PIONEX_BALANCE_POLL_SEC,
    MAX_FUNDING_RATE_ABS, MAX_SPREAD_PCT, MAX_ENTRY_SIGNAL_DRIFT_PCT,
    MARK_FILL_SANITY_PCT, EXCHANGE_MARK_NUDGE_PCT, MAX_EXCHANGE_PROTECTION_NUDGE_PCT,
    SYMBOL_FILTER_MODES,
    SYMBOL_FILTER_DEFAULT_MIN_TRADES,
    SYMBOL_FILTER_DEFAULT_MIN_WIN_RATE,
    SYMBOL_FILTER_DEFAULT_MIN_NET_PNL,
    SYMBOL_FILTER_ROLLING_WINDOW,
    TAB17_BASE_UNIVERSE, TAB17_MOMENTUM_TOP_N, TAB17_MIN_PRICE_CHG_PCT,
    TAB17_MIN_VOL_SPIKE_MULT, TAB17_VOL_SMA_LEN, TAB17_VOL_RATIO_CAP, TAB17_MAX_POS,
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

'''

SCAN_REGISTRY = '''
            if interval == "4h":
                for tab_name, evaluator, post in TAB_EVALUATORS_4H:
                    if can_collect(tab_name, sym, signal_ts):
                        collect_candidate(
                            tab_name,
                            sym,
                            signal_ts,
                            evaluate_tab_signal(tab_name, evaluator, post, df),
                        )

            if interval == "1h":
                for tab_name, evaluator, post in TAB_EVALUATORS_1H:
                    if post == "tab17_momentum":
                        if not (tab17_universe and sym in tab17_universe and can_collect(tab_name, sym, signal_ts)):
                            continue
                        collect_candidate(
                            tab_name,
                            sym,
                            signal_ts,
                            evaluate_tab_signal(tab_name, evaluator, None, df),
                            momentum_score=tab17_universe[sym],
                        )
                    elif can_collect(tab_name, sym, signal_ts):
                        collect_candidate(
                            tab_name,
                            sym,
                            signal_ts,
                            evaluate_tab_signal(tab_name, evaluator, post, df),
                        )
'''

LOAD_FRESH = '''        state = default_state(
            tab_enabled=_startup_tab_enabled(),
            margin_size=_DEFAULT_MARGIN_SIZE,
            tab17_risk_row=_default_tab17_risk_row(),
        )
        return'''

LOAD_CORRUPT = '''        state = default_state_corrupt_recovery(
            default_state(
                tab_enabled=_startup_tab_enabled(),
                margin_size=_DEFAULT_MARGIN_SIZE,
                tab17_risk_row=_default_tab17_risk_row(),
            )
        )
        return'''

FOOTER = '''
_logmod.register_log_hooks(send_telegram=send_telegram, record_error_event=record_error_event)
'''


def main() -> None:
    lines = _read_git_server().splitlines(keepends=True)
    state_i = next(i for i, l in enumerate(lines) if l.startswith("# --- PAPER TRADING STATE ---"))
    main_i = next(i for i, l in enumerate(lines) if l.startswith("if __name__"))

    body = "".join(lines[state_i:main_i])

    # load_state: replace inline default dicts with schema helpers
    body = re.sub(
        r"    if not os\.path\.exists\(STATE_FILE\):\n        state = \{.*?\n        \}\n        return",
        f"    if not os.path.exists(STATE_FILE):\n{LOAD_FRESH}",
        body,
        count=1,
        flags=re.DOTALL,
    )
    body = re.sub(
        r"        state = \{\n            \"balances\".*?\n        state\[\"balances\"\]\[\"Recovered\"\] = 0\.0\n        return",
        LOAD_CORRUPT,
        body,
        count=1,
        flags=re.DOTALL,
    )

    # scan_candle_signals: tab if-blocks -> registry dispatch
    body = re.sub(
        r"            if interval == \"4h\":\n.*?                if can_collect\(\"Tab18\".*?\n                    \)\n",
        SCAN_REGISTRY + "\n",
        body,
        count=1,
        flags=re.DOTALL,
    )

    OUT.write_text(HEADER + body + FOOTER, encoding="utf-8")
    print(f"Wrote {OUT} ({len((HEADER + body + FOOTER).splitlines())} lines)")


if __name__ == "__main__":
    main()
