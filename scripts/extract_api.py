"""Extract FastAPI app, lifespan, and REST routes into bot/api/web.py."""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from domain_extract import (
    build_import_block,
    collect_exports,
    function_names,
    slice_by_markers,
    transform_chunk,
)

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "bot" / "core.py"
OUT = ROOT / "bot" / "api" / "web.py"
IMPORT_CHECK = "from bot.api.web import"

HEADER = '''"""FastAPI dashboard app and REST routes (extracted from bot.core)."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

import binance_live
import pionex_live
import strategies
from config import DASHBOARD_ALLOWED_ORIGINS


def _core():
    from bot import core
    return core


'''

CONFIG_VIA_CORE = (
    "LIVE_MODE",
    "DASHBOARD_AUTH_ENABLED",
    "DASHBOARD_PASSCODE",
    "BINANCE_TESTNET",
    "TELEGRAM_ENABLED",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "ENTRY_EVAL_BUDGET_SEC",
    "ENTRY_BUSY_BUFFER_SEC",
    "BINANCE_CLOSE_HISTORY_ENABLED",
    "PIONEX_CONFIGURED",
    "TABS",
    "INITIAL_BALANCE",
    "HISTORY_CAP",
    "MAX_POSITIONS_PER_TAB",
    "NOTIONAL_SIZE",
    "LEVERAGE",
    "SLTP_MODE",
    "SLTP_MODES",
    "LOCAL_SLTP",
    "SYMBOL_SCAN_LIMIT",
    "STARTUP_ENABLED_TABS",
    "CLOSE_ALL_PREFLIGHT",
    "CLOSE_ALL_RETRY_SEC",
    "CLOSE_ALL_STAGGER_SEC",
    "MIN_MARGIN_SIZE",
    "MAX_MARGIN_SIZE",
)

CORE_ATTRS = (
    "_http_client",
    "_telegram_client",
    "_EMERGENCY_CLOSE_RETRY_TASK",
    "_EMERGENCY_CLOSE_RETRY_PENDING",
    "_state_lock",
    "_eval_lock",
    "exchange_account",
    "latest_marks",
    "latest_prices",
    "SCAN_SYMBOLS",
    "_pionex_balance_snapshot",
    "_error_event_recent",
    "_DASHBOARD_MARKET_SYMBOLS",
    "_DEFAULT_MARGIN_SIZE",
    "MAX_POSITIONS_OPTIONS",
    "NOTIONAL_SIZE_OPTIONS",
    "LEVERAGE_OPTIONS",
    "MARGIN_SIZE_OPTIONS",
    "SYMBOL_SCAN_OPTIONS",
)

BARE_CORE_CALLS = (
    "fetch_scan_symbols",
    "refresh_scan_symbols_loop",
    "binance_ws_loop",
    "price_tick_monitor_loop",
    "price_poll_loop",
    "scheduler_loop",
    "process_watchdog_loop",
    "server_time_sync_loop",
    "sync_live_positions",
    "binance_close_history_loop",
    "user_data_stream_loop",
    "exchange_account_loop",
    "income_sync_loop",
    "entry_retry_loop",
    "pionex_balance_loop",
    "save_state",
    "load_state",
    "record_error_event",
    "send_telegram",
    "close_position",
)

WEB_VIA_CORE = (
    "_schedule_emergency_close_retry",
    "_finalize_closed_positions",
    "_emergency_close_retry_worker",
)


def _postprocess_web_module(module: str) -> str:
    module = module.replace(
        "allow_origins=c.DASHBOARD_ALLOWED_ORIGINS",
        "allow_origins=DASHBOARD_ALLOWED_ORIGINS",
    )
    for name in BARE_CORE_CALLS:
        module = re.sub(
            rf"(?<!c\.)(?<![.\w]){re.escape(name)}\(",
            f"c.{name}(",
            module,
        )
    for name in WEB_VIA_CORE:
        module = re.sub(
            rf"(?<!def )(?<!async )(?<!c\.)(?<![.\w]){re.escape(name)}\(",
            f"c.{name}(",
            module,
        )
    # Route handlers sit below @app.* decorators — inject c = _core() after each def line.
    module = re.sub(
        r"^(@app\.(?:get|post|websocket)\([^\n]*\nasync def [a-zA-Z_]\w+\([^\n]*\):)\n",
        r"\1\n    c = _core()\n",
        module,
        flags=re.MULTILINE,
    )
    module = re.sub(
        r"^(async def api_[a-zA-Z_]\w+\([^\n]*\):)\n",
        r"\1\n    c = _core()\n",
        module,
        flags=re.MULTILINE,
    )
    module = re.sub(
        r"^(async def dashboard_ws\([^\n]*\):)\n",
        r"\1\n    c = _core()\n",
        module,
        flags=re.MULTILINE,
    )
    module = re.sub(
        r"^(async def get_dashboard\([^\n]*\):)\n",
        r"\1\n    c = _core()\n",
        module,
        flags=re.MULTILINE,
    )
    return module


def main() -> None:
    text = CORE.read_text(encoding="utf-8")
    if IMPORT_CHECK in text:
        print("Skip api/web.py — already imported in core")
        return

    lines = text.splitlines(keepends=True)
    chunk, start, end = slice_by_markers(
        lines,
        "# --- FASTAPI APP ---",
        "def _acquire_single_instance_lock",
    )
    chunk = chunk.split("# --- FASTAPI APP ---", 1)[-1].lstrip("\n")

    local_funcs = function_names(chunk)
    module = HEADER + _postprocess_web_module(
        transform_chunk(
            chunk,
            local_funcs,
            core_attrs=CORE_ATTRS,
            config_via_core=CONFIG_VIA_CORE,
            preserve_nested_calls=True,
        )
    ).rstrip() + "\n"

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(module, encoding="utf-8")

    exports = collect_exports(chunk)
    exports.append("app")
    import_block = build_import_block("bot.api.web", exports)

    new_lines = lines[:start] + lines[end:]
    idx = next(i for i, l in enumerate(new_lines) if l.startswith("def _acquire_single_instance_lock"))
    new_lines = new_lines[:idx] + [import_block] + new_lines[idx:]

    CORE.write_text("".join(new_lines), encoding="utf-8")
    print(f"Wrote {OUT} ({len(module.splitlines())} lines, {len(exports)} exports)")


if __name__ == "__main__":
    main()
