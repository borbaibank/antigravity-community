"""One-shot mechanical split of server.py into bot/ package (behavior-preserving move-only)."""

from __future__ import annotations

import ast
import os
import re
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"

# Function name -> target module (relative to bot/)
MODULE_MAP: dict[str, str] = {}

# Prefix / name rules (first match wins)
RULES: list[tuple[str, str]] = [
    (r"^load_state$|^save_state$|^_write_state$|^_state_write_guard", "state/persistence.py"),
    (r"^_effective_|^_normalize_|^_default_|^_startup_tab|^_|^_clamp_|^_sync_sizing|^_|^_match_float|^_|^_tab_risk|^_|^_tab_max|^_|^_tab_on$|^_|^_symbols_for|^_|^_interval_has|^_|^_scan_gate|^_|^_entry_gate|^_|^_symbol_filter|^_|^_rolling_symbol|^_|^_all_rolling|^_|^_rebuild_tab_stats|^_|^_rebuild_symbol_stats|^_|^_record_tab_stats", "state/accessors.py"),
    (r"^scan_candle|^evaluate_candle|^execute_scanned|^_|^_open_balanced|^_|^_defer_entry|^_|^_partition_due|^_|^_retry_one|^_|^_queue_|^_entry_retry|^_|^_execute_entry|^_|^_execute_scanned|^_|^_entry_|^_|^_pre_entry|^_|^_resolve_live_entry|^_|^_cleanup_failed|^_|^_entry_long_short|^_|^_entry_size|^_|^_check_entry|^_|^_tab17_momentum|^_|^_build_tab17", "engine/entry.py"),
    (r"^execute_entry$|^close_position$|^_|^_close_position|^_|^_emergency_close|^_|^_dashboard_emergency|^_|^_schedule_emergency|^_|^_finalize_exit|^_|^_log_entry|^_|^_apply_history_pnl", "engine/exit.py"),
    (r"^_planned_protection|^_|^_protection_prices|^_|^_validate_planned|^_|^_check_local_position|^_|^_verify_live_position|^_|^_recover_untracked|^_|^_exchange_sl_crossed|^_|^_is_immediate_trigger|^_|^_position_should_use_local|^_|^_missing_exchange_protection|^_|^_vanished_exchange", "engine/protection.py"),
    (r"^sync_live_positions$|^purge_orphaned|^_|^_fetch_live_qty|^_|^_repair_|^_|^_recover_|^_|^_sync_issue|^_|^_prune_sync|^_|^_dedupe_sync", "engine/sync.py"),
    (r"^check_invalidations", "engine/invalidation.py"),
    (r"^_reconcile_pnl|^_|^_repair_history|^_|^_refresh_binance_close|^_|^_sync_today|^_|^_sync_daily|^_|^_rebuild_binance_gross|^_|^_group_income|^_|^_summarize_income|^_|^_attribute_income|^_|^_BINANCE_GROSS|^_|^_effective_tab_gross|^_|^_apply_history|^_|^_match_close|^_|^_resolve_close|^_|^_trade_commission|^_|^_fill_net_pnl|^_|^_order_fill_commission|^_|^_income_sync|^_|^income_sync_loop$|^_|^_normalize_binance_income|^_|^_normalize_binance_tab", "engine/pnl.py"),
    (r"^get_klines$|^fetch_scan|^refresh_scan|^_|^_kline_|^_|^_symbols_for_interval|^_|^_fetch_mark|^_|^_mark_or_last|^_|^_position_price|^_|^_premium_index|^_|^_note_binance_rate|^_|^_binance_rate", "feeds/klines.py"),
    (r"^binance_ws_loop$|^price_tick_monitor|^price_poll_loop$|^_|^_process_binance_price|^_|^_apply_mark_price", "feeds/price_ws.py"),
    (r"^handle_order_update$|^user_data_stream_loop$|^_|^_apply_uds|^_|^_uds_position", "feeds/uds.py"),
    (r"^scheduler_loop$|^exchange_account_loop$|^process_watchdog_loop$|^server_time_sync_loop$|^entry_retry_loop$|^binance_close_history_loop$|^pionex_balance_loop$|^_|^_seconds_until|^_|^_begin_entry|^_|^_release_entry|^_|^_mark_entry_busy|^_|^_entry_busy|^_|^_is_entry_window|^_|^_should_defer_pnl", "scheduler/loops.py"),
    (r"^send_telegram$|^record_error_event$|^_|^_telegram_allowed|^_|^_health_snapshot|^_|^_position_protection_risk|^_|^_expected_local", "engine/ops.py"),
    (r"^_|^_position_side|^_|^_position_tuple|^_|^_algo_|^_|^_strategy_client|^_|^_live_key|^_|^_hedge|^_|^_sibling|^_|^_upsert_position|^_|^_position_registry|^STRATEGY_LABELS", "state/position_identity.py"),
    (r"^_|^_dashboard_|^_build_dashboard|^_|^_recalculate_unrealized|^_|^_refresh_exchange_account|^_|^_pnl_summary|^_|^_effective_tab_gross", "api/dashboard.py"),
]

# Constants / module-level assignments at start of state section
CONST_MODULE = "state/constants.py"


def classify(name: str) -> str:
    if name in MODULE_MAP:
        return MODULE_MAP[name]
    for pat, mod in RULES:
        if re.search(pat, name):
            return mod
    return "core/misc.py"


def main() -> None:
    src = SERVER.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)

    # Find FASTAPI section
    fastapi_line = next(i for i, l in enumerate(lines) if l.startswith("# --- FASTAPI APP ---"))
    core_end = fastapi_line

    # Split: header (imports through logging), core body, api body
    # Find PAPER TRADING STATE
    state_line = next(i for i, l in enumerate(lines) if l.startswith("# --- PAPER TRADING STATE ---"))

    header = "".join(lines[:state_line])
    core_body = "".join(lines[state_line:core_end])
    api_body = "".join(lines[fastapi_line:])

    bot = ROOT / "bot"
    for sub in (
        "state",
        "engine",
        "feeds",
        "scheduler",
        "api",
        "api/routes",
        "core",
    ):
        (bot / sub).mkdir(parents=True, exist_ok=True)
        init = bot / sub / "__init__.py"
        if not init.exists():
            init.write_text('"""Bot subpackage."""\n', encoding="utf-8")

    # Parse top-level defs in core_body
    core_tree = ast.parse(core_body)
    chunks: dict[str, list[str]] = {}
    module_globals: dict[str, list[str]] = {CONST_MODULE: []}

    items = list(core_tree.body)
    i = 0
    while i < len(items):
        node = items[i]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
            mod = classify(name)
            chunk = "".join(lines[state_line + node.lineno - 1 : state_line + items[i + 1].lineno - 1 if i + 1 < len(items) else core_end])
            # Fix: use line numbers from core_body only
            start = node.lineno - 1
            end = items[i + 1].lineno - 1 if i + 1 < len(items) else len(core_body.splitlines())
            chunk_lines = core_body.splitlines(keepends=True)[start:end]
            chunks.setdefault(mod, []).append("".join(chunk_lines))
            i += 1
        elif isinstance(node, ast.ClassDef):
            mod = "core/misc.py"
            start = node.lineno - 1
            end = items[i + 1].lineno - 1 if i + 1 < len(items) else len(core_body.splitlines())
            chunk_lines = core_body.splitlines(keepends=True)[start:end]
            chunks.setdefault(mod, []).append("".join(chunk_lines))
            i += 1
        elif isinstance(node, ast.Assign) or isinstance(node, ast.AnnAssign):
            start = node.lineno - 1
            end = items[i + 1].lineno - 1 if i + 1 < len(items) else node.lineno
            chunk_lines = core_body.splitlines(keepends=True)[start:end]
            # First batch of assigns -> constants
            mod = CONST_MODULE if node.lineno < 650 else "core/runtime.py"
            module_globals.setdefault(mod, []).append("".join(chunk_lines))
            i += 1
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            # load_state() at module level
            chunk_lines = core_body.splitlines(keepends=True)[node.lineno - 1 : node.end_lineno]
            module_globals.setdefault("core/runtime.py", []).append("".join(chunk_lines))
            i += 1
        else:
            i += 1

    COMMON_HEADER = textwrap.dedent(
        '''
        """Auto-extracted from server.py — move-only."""
        from __future__ import annotations

        import asyncio
        import json
        import os
        import re
        import time
        from collections import deque
        from datetime import datetime, timezone, timedelta

        import httpx
        import pandas as pd
        import websockets

        import binance_live
        import pionex_live
        import strategies
        from bot import logging_setup as _logmod
        from bot.state import constants as _c
        from bot.state import runtime as _rt
        from config import *

        state = _rt.state
        '''
    ).strip() + "\n\n"

    for mod, parts in {**chunks, **{k: v for k, v in module_globals.items() if v}}.items():
        path = bot / mod
        path.parent.mkdir(parents=True, exist_ok=True)
        content = COMMON_HEADER + "\n".join(parts)
        path.write_text(content, encoding="utf-8")
        print(f"Wrote {path} ({len(parts)} chunks)")

    print("Done — manual fixups required for imports and server.py facade")


if __name__ == "__main__":
    main()
