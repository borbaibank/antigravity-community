"""Split bot/core.py into domain modules (exec into shared namespace — behavior-preserving).

DEPRECATED: AST regex classifier mis-assigns functions (bare `^_` matches everything).
Use scripts/restore_core.py to rebuild the working monolith instead.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "bot" / "core.py"

# Function/class name -> chunk file (under bot/)
ASSIGNMENTS: list[tuple[str, str]] = [
    (r"^load_state$|^save_state$|^_write_state$|^_state_write_guard", "state/persistence.py"),
    (r"^_effective_|^_normalize_|^_default_tab17|^_|^_clamp_tab|^_|^_tab_risk|^_|^_sync_sizing|^_|^_match_float|^_|^_startup_tab|^_|^_is_winning|^_|^_empty_tab_stats|^_|^_accumulate_tab|^_|^_rebuild_tab_stats|^_|^_record_tab_stats|^_|^_empty_symbol_stats|^_|^_record_symbol_stats|^_|^_rebuild_symbol_stats|^_|^_invalidate_rolling|^_|^_rolling_symbol|^_|^_all_rolling|^_|^_default_symbol_filter|^_|^_clamp_symbol_filter|^_|^_symbol_in_blocklist|^_|^_symbol_passes|^_|^_symbol_entry_allowed|^_|^_auto_winner|^_|^_symbol_leaderboard|^_|^_dashboard_symbol_leaderboard|^_|^_rebuild_tab_stats_for|^_|^_binance_close_history_enabled$|^_use_binance_close_cache$", "state/accessors.py"),
    (r"^_algo_|^_position_side|^_|^_position_tuple|^_|^_strategy_client|^_|^_strategy_from|^_|^_strategy_role|^_|^_order_client|^_|^_record_bot_close|^_|^_prune_recent_bot|^_|^_has_recent_bot|^_|^_consume_recent_bot|^_|^_strategy_from_algo|^STRATEGY_LABELS$|^_strategy_label$", "state/position_identity.py"),
    (r"^send_telegram$|^record_error_event$|^_telegram_|^_is_tp_sl_exit|^_|^_health_snapshot|^_|^_expected_local_protection|^_|^_position_protection_risk|^_|^_mark_process_ok|^_|^_note_error", "engine/ops.py"),
    (r"^_planned_protection|^_|^_protection_prices|^_|^_validate_planned|^_|^_check_local_position|^_|^_verify_live_position|^_|^_recover_untracked|^_|^_exchange_sl_crossed|^_|^_is_immediate_trigger|^_|^_position_should_use_local|^_|^_missing_exchange_protection|^_|^_vanished_exchange|^_|^_position_sl_is|^_|^_position_tp_is|^_|^_position_full_local|^_|^_position_needs_local|^_|^_resolve_entry_protection|^_|^_apply_protection_sources|^_|^_policy_cancels|^_|^_normalize_sltp|^_|^_default_sltp|^_|^_effective_sltp|^_|^_effective_local_sltp|^_|^_max_open_algo|^_|^_sltp_diff|^_|^_mark_ref_for_exchange|^_|^_clamp_exchange_nudge", "engine/protection.py"),
    (r"^_reconcile_pnl|^_|^_repair_history|^_|^_refresh_binance_close|^_|^_sync_today|^_|^_sync_daily|^_|^_rebuild_binance_gross|^_|^_group_income|^_|^_summarize_income|^_|^_attribute_income|^_|^_normalize_binance_income|^_|^_normalize_binance_tab|^_|^_effective_tab_gross|^_|^_apply_history_pnl|^_|^_match_close_trades|^_|^_resolve_close_order|^_|^_trade_commission|^_|^_fill_net_pnl|^_|^_order_fill_commission|^income_sync_loop$|^_|^_bootstrap_daily|^_|^_repair_history_sltp|^_|^_repair_history_pnl|^_|^_reconcile_pnl|^_|^_refresh_binance_close|^_|^_BINANCE_GROSS|^_|^_enrich_history|^_|^_dashboard_recent_history|^_|^_sync_income|^_|^_record_income|^_|^_gross_breakdown|^_|^_fetch_income|^_|^_tab_income", "engine/pnl.py"),
    (r"^close_position$|^_close_position_unsafe$|^_emergency_close|^_|^_dashboard_emergency_close|^_|^_schedule_emergency_close|^_|^_finalize_exit|^_|^_log_entry|^_|^_apply_close|^_|^_close_live|^_|^_resolve_live_qty|^_|^_fetch_live_qty|^_|^_sibling_qty|^_|^_cap_close|^_|^_bot_close|^_|^_manual_close|^record_exchange_sync_close$|^_|^_history_has_sync|^_|^_exit_target|^_|^_exit_move", "engine/exit.py"),
    (r"^scan_candle_signals$|^execute_scanned_entries$|^evaluate_candle_signals$|^execute_entry$|^_execute_entry_unsafe$|^_execute_entry|^_|^_open_balanced|^_|^_defer_entry|^_|^_partition_due_entry|^_|^_retry_one_queued|^_|^_requeue_deferred|^_|^_entry_retry|^_|^_entry_gate|^_|^_entry_busy|^_|^_entry_window|^_|^_begin_entry|^_|^_release_entry|^_|^_mark_entry_busy|^_|^_is_entry_window|^_|^_entry_long_short|^_|^_entry_size|^_|^_check_entry|^_|^_pre_entry|^_|^_resolve_live_entry|^_|^_cleanup_failed_live|^_|^_entry_price|^_|^_pending_entry|^_|^_queue_deferred|^_|^_tab17_momentum|^_|^_build_tab17|^_|^_tab_on$|^_|^_tab_max_positions|^_|^_reset_entry_stagger|^_|^_entry_stagger|^_|^_scan_gate|^_|^_collect_candidate", "engine/entry.py"),
    (r"^sync_live_positions$|^purge_orphaned|^_|^_sync_issue|^_|^_prune_sync|^_|^_dedupe_sync|^_|^_repair_orphan|^_|^_recover_orphan|^_|^_stale_position|^_|^_orphan_algo|^_|^_live_sync|^_|^_remove_stale|^_|^_qty_mismatch|^_|^_sync_close|^_|^_apply_sync", "engine/sync.py"),
    (r"^check_invalidations_loop$|^_interval_candle_closed|^_|^_invalidation", "engine/invalidation.py"),
    (r"^get_klines$|^fetch_scan_symbols$|^refresh_scan_symbols_loop$|^_fetch_mark_price$|^_mark_or_last$|^_position_price$|^_premium_index|^_|^_note_binance_rate|^_|^_binance_rate_limit|^_|^_kline_fetch|^_|^_effective_kline_fetch_delay|^_|^_scan_universe|^_|^_interval_has_enabled|^_|^_symbols_for_interval|^_|^_kline_request_weight$|^_clamp_symbol_scan|^_|^_effective_symbol_scan", "feeds/klines.py"),
    (r"^binance_ws_loop$|^price_tick_monitor_loop$|^price_poll_loop$|^_process_binance_price|^_|^_apply_mark_price|^_|^_refresh_exchange_account_marks", "feeds/price_ws.py"),
    (r"^handle_order_update$|^user_data_stream_loop$|^_apply_uds|^_|^_uds_position|^_|^_fill_net_pnl_from_uds", "feeds/uds.py"),
    (r"^scheduler_loop$|^exchange_account_loop$|^process_watchdog_loop$|^server_time_sync_loop$|^entry_retry_loop$|^binance_close_history_loop$|^pionex_balance_loop$|^_seconds_until_next_candle|^_|^_should_defer_pnl|^_|^_pnl_repair|^_|^_watchdog|^_|^_exchange_account_snapshot|^_|^_refresh_exchange_account$|^_|^_sync_exchange|^_|^_margin_history_sample|^_|^_record_margin", "scheduler/loops.py"),
    (r"^_build_dashboard|^_|^_dashboard_|^_recalculate_unrealized|^_|^_pnl_summary|^_|^_effective_tab_gross_for|^_|^_dashboard_exchange|^_|^_dashboard_pnl|^_|^_dashboard_health|^_|^_dashboard_algo|^_|^_stats_trade|^_|^_dashboard_ws|^_|^_serialize_dashboard", "api/dashboard.py"),
    (r"^lifespan$|^app$|^_|^_check_auth$|^_|^_acquire_single_instance|^api_|^_|^_emergency_close_batch$", "api/app.py"),
]

CHUNK_ORDER = [
    "state/constants.py",
    "state/accessors.py",
    "state/persistence.py",
    "state/position_identity.py",
    "engine/ops.py",
    "engine/protection.py",
    "engine/pnl.py",
    "engine/exit.py",
    "engine/entry.py",
    "engine/sync.py",
    "engine/invalidation.py",
    "feeds/klines.py",
    "feeds/price_ws.py",
    "feeds/uds.py",
    "scheduler/loops.py",
    "api/dashboard.py",
    "api/app.py",
    "core/misc.py",
]


def classify(name: str) -> str:
    for pat, mod in ASSIGNMENTS:
        if re.search(pat, name):
            return mod
    return "core/misc.py"


def main() -> None:
    src = CORE.read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)

    # Header: through _PROJECT_ROOT (before PAPER TRADING STATE body)
    header_end = next(i for i, l in enumerate(lines) if l.startswith("# --- PAPER TRADING STATE ---"))
    header = "".join(lines[:header_end])

    chunks: dict[str, list[str]] = {c: [] for c in CHUNK_ORDER}

    # Module-level constants block (between PAPER TRADING STATE marker and first def)
    const_start = header_end
    first_def_line = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            first_def_line = node.lineno
            break
    if first_def_line:
        const_body = "".join(lines[const_start : first_def_line - 1])
        chunks["state/constants.py"].append(const_body)

    items = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    # also handle module-level assigns and exprs after constants
    all_nodes = tree.body
    idx = 0
    while idx < len(all_nodes):
        node = all_nodes[idx]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
            mod = classify(name)
            start = node.lineno - 1
            end = all_nodes[idx + 1].lineno - 1 if idx + 1 < len(all_nodes) else len(lines)
            # extend to include decorators
            if node.decorator_list:
                start = min(d.lineno for d in node.decorator_list) - 1
            chunk_text = "".join(lines[start:end])
            chunks[mod].append(chunk_text)
            idx += 1
        elif isinstance(node, ast.Assign) and node.lineno >= (first_def_line or 99999):
            # assigns after functions - put in misc
            start = node.lineno - 1
            end = all_nodes[idx + 1].lineno - 1 if idx + 1 < len(all_nodes) else len(lines)
            chunks["core/misc.py"].append("".join(lines[start:end]))
            idx += 1
        elif isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Call):
            # load_state() at import, print startup
            start = node.lineno - 1
            end = node.end_lineno or node.lineno
            chunks["core/misc.py"].append("".join(lines[start:end]))
            idx += 1
        else:
            idx += 1

    # Runtime globals block: between load_state() call and FASTAPI
    try:
        fastapi_line = next(i for i, l in enumerate(lines) if l.startswith("# --- FASTAPI APP ---"))
        # find load_state() call
        load_call = next(i for i, l in enumerate(lines) if l.strip() == "load_state()" and i < fastapi_line)
        runtime_block = "".join(lines[load_call + 1 : fastapi_line])
        chunks["core/runtime_globals.py"] = [runtime_block]
    except StopIteration:
        pass

    bot = ROOT / "bot"
    for rel, parts in chunks.items():
        if not parts:
            continue
        path = bot / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "".join(parts)
        path.write_text(
            f'"""\nAuto-split from core.py — executed in bot.core namespace.\nSource: {rel}\n"""\n\n{body}',
            encoding="utf-8",
        )
        print(f"Wrote {path} ({len(body.splitlines())} lines)")

    # Write core assembly
    loader_chunks = [c for c in CHUNK_ORDER if (bot / c).exists()]
    if (bot / "core/runtime_globals.py").exists():
        loader_chunks.append("core/runtime_globals.py")

    assembly = header + '''

def _load_domain_chunks() -> None:
    """Load split modules into this namespace (move-only; preserves single-module semantics)."""
    import importlib.util
    from pathlib import Path
    _root = Path(__file__).resolve().parent
    _order = ''' + repr(loader_chunks) + '''
    _ns = globals()
    for _rel in _order:
        _path = _root / _rel
        if not _path.exists():
            continue
        _spec = importlib.util.spec_from_file_location(f"bot.{_rel.replace('/', '.')}", _path)
        if _spec is None or _spec.loader is None:
            continue
        _code = _path.read_text(encoding="utf-8")
        _mod = importlib.util.module_from_spec(_spec)
        # Execute with core namespace so `state`, locks, and cross-refs stay unified.
        exec(compile(_code, str(_path), "exec"), _ns, _ns)

_load_domain_chunks()

_logmod.register_log_hooks(send_telegram=send_telegram, record_error_event=record_error_event)
'''

    # Backup and write - we need to extract header-only from current core and replace body
    backup = bot / "core_monolith.py.bak"
    if not backup.exists():
        backup.write_text(src, encoding="utf-8")

    # For assembly, header should NOT include functions - only imports through _print_startup_banner
    # Re-read: header in our split is lines before PAPER TRADING STATE - includes _print_startup_banner which calls _effective_sltp not yet defined!
    # Original core had same issue - _print_startup_banner defined before accessors but called at runtime only.

    new_core = assembly
    # Remove duplicate: header already has imports; assembly adds _load_domain_chunks
    # Fix: header should end before _print_startup_banner OR banner in misc chunk

    CORE.write_text(new_core, encoding="utf-8")
    print(f"Wrote {CORE} assembly ({len(new_core.splitlines())} lines)")


if __name__ == "__main__":
    main()
