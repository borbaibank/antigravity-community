"""Paper state load/save — behavior-preserving extract from bot.core."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone

from bot.state.schema import default_state, default_state_corrupt_recovery
from config import (
    INITIAL_BALANCE,
    LIVE_MODE,
    MAX_NOTIONAL_SIZE,
    MAX_POSITIONS_PER_TAB,
    SLTP_MODE,
    SYMBOL_SCAN_LIMIT,
    TABS,
)
from bot.engine.premium_config import tab17_base_universe


def _core():
    from bot import core
    return core


def load_state():
    c = _core()
    if not os.path.exists(c.STATE_FILE):
        c.state = default_state(
            tab_enabled=c._startup_tab_enabled(),
            margin_size=c._DEFAULT_MARGIN_SIZE,
        )
        return

    try:
        with open(c.STATE_FILE, "r") as f:
            c.state = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        # Backup corrupted file before wiping
        import shutil, time as _time
        backup = c.STATE_FILE + f".corrupt.{int(_time.time())}"
        try:
            shutil.copy2(c.STATE_FILE, backup)
            print(f"[WARN] State file corrupted ({e}). Backup saved to {backup}. Starting fresh.")
        except Exception:
            print(f"[WARN] State file unreadable ({e}), starting fresh.")
        c.state = default_state_corrupt_recovery(
            default_state(
                tab_enabled=c._startup_tab_enabled(),
                margin_size=c._DEFAULT_MARGIN_SIZE,
            )
        )
        return

    if "used_setups" not in c.state: c.state["used_setups"] = []
    if "balances" not in c.state:
        c.state["balances"] = {tab: c.state.get("balance", INITIAL_BALANCE) for tab in TABS}
    else:
        for tab in TABS:
            c.state["balances"].setdefault(tab, INITIAL_BALANCE)
    # Circuit-breaker persistence (added fix 2)
    if "circuit_breaker" not in c.state:
        c.state["circuit_breaker"] = False
    if "daily_loss_usd" not in c.state:
        c.state["daily_loss_usd"] = 0.0
    if "daily_loss_date" not in c.state:
        c.state["daily_loss_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if "sync_issues" not in c.state:
        c.state["sync_issues"] = []
    if "error_events" not in c.state:
        c.state["error_events"] = []
    if "position_registry" not in c.state or not isinstance(c.state.get("position_registry"), dict):
        c.state["position_registry"] = {}
    if "pending_entry_orders" not in c.state or not isinstance(c.state.get("pending_entry_orders"), dict):
        c.state["pending_entry_orders"] = {}
    if "margin_history" not in c.state:
        c.state["margin_history"] = []
    if "equity_snapshots" not in c.state:
        c.state["equity_snapshots"] = []
    if "prescreen_watchlists" not in c.state or not isinstance(c.state.get("prescreen_watchlists"), dict):
        c.state["prescreen_watchlists"] = {}
    if "binance_income" not in c.state:
        # Cumulative totals reconciled against Binance /fapi/v1/income.
        # Sum of (realized_pnl + commission + funding) == Binance's total realized PnL.
        # Seed last_ts to now so we only count income from bot start onwards,
        # not Binance's ~7-day default history window.
        _seed_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        c.state["binance_income"] = {
            "realized_pnl": 0.0,
            "commission":   0.0,
            "funding":      0.0,
            "last_ts":      _seed_ts,
            "seed_ts":      _seed_ts,
            "gross_profit": 0.0,
            "gross_loss":   0.0,
            "seen_tran_ids": [],
            "gross_rebuilt": False,
        }
    if LIVE_MODE:
        inc = c.state.setdefault("binance_income", {})
        inc.setdefault("gross_profit", 0.0)
        inc.setdefault("gross_loss", 0.0)
        inc.setdefault("seen_tran_ids", [])
        inc.setdefault("gross_rebuilt", False)
        if "seed_ts" not in inc:
            inc["seed_ts"] = int(inc.get("last_ts", 0) or 0)
        tabs = c.state.setdefault("binance_tab_income", {})
        for tab in list(TABS) + ["SafeGuard", "Recovered"]:
            if tab not in tabs or not isinstance(tabs.get(tab), dict):
                tabs[tab] = {"gross_profit": 0.0, "gross_loss": 0.0}
            else:
                tabs[tab].setdefault("gross_profit", 0.0)
                tabs[tab].setdefault("gross_loss", 0.0)
    # Per-tab on/off switch. Preserve the last saved dashboard c.state across restarts.
    c.state["tab_enabled"] = c._normalize_tab_enabled(c.state.get("tab_enabled"))
    if "long_short_balance_mode" not in c.state:
        c.state["long_short_balance_mode"] = "nearly" if c.state.get("long_short_balance_enabled", False) else "off"
    if c.state.get("long_short_balance_mode") not in {"nearly", "cap", "off"}:
        c.state["long_short_balance_mode"] = "off"
    c.state.pop("long_short_balance_enabled", None)
    if c.state.get("trade_side_mode") not in {"both", "long_only", "short_only"}:
        c.state["trade_side_mode"] = "both"
    try:
        mp = int(c.state.get("max_positions_per_tab", MAX_POSITIONS_PER_TAB))
    except (TypeError, ValueError):
        mp = MAX_POSITIONS_PER_TAB
    if mp not in c.MAX_POSITIONS_OPTIONS:
        mp = MAX_POSITIONS_PER_TAB
    c.state["max_positions_per_tab"] = mp
    c._normalize_sizing_state()
    print(
        f"[Risk] env sizing margin_size={c.state.get('margin_size')} "
        f"leverage={c.state.get('leverage')} notional_size={c.state.get('notional_size')}"
    )
    cap = float(MAX_NOTIONAL_SIZE or 0)
    if cap > 0:
        try:
            loaded_ns = float(c.state.get("notional_size") or 0)
        except (TypeError, ValueError):
            loaded_ns = 0.0
        if loaded_ns > cap + 1e-6:
            print(
                f"[Risk] WARN notional_size {loaded_ns:.2f} exceeds MAX_NOTIONAL_SIZE "
                f"{cap:.2f} — new entries blocked until lowered"
            )
    try:
        ssl = int(c.state.get("symbol_scan_limit", SYMBOL_SCAN_LIMIT))
    except (TypeError, ValueError):
        ssl = SYMBOL_SCAN_LIMIT
    if ssl not in c.SYMBOL_SCAN_OPTIONS:
        ssl = c._clamp_symbol_scan_limit(ssl) or SYMBOL_SCAN_LIMIT
    c.state["symbol_scan_limit"] = ssl
    c.state["symbol_scan_limit_by_tab"] = c._normalize_symbol_scan_limit_by_tab()
    by_tab = c.state["symbol_scan_limit_by_tab"]
    if "Tab17" in TABS and "Tab17" not in by_tab:
        cap = tab17_base_universe()
        if cap is not None:
            by_tab["Tab17"] = cap
            c.state["symbol_scan_limit_by_tab"] = c._normalize_symbol_scan_limit_by_tab()
    c.state.pop("tab_risk_by_tab", None)
    c.state["sltp_mode"] = c._normalize_sltp_mode(SLTP_MODE)
    # Always reset unrealized PnL
    c.state["unrealized_pnls"] = {tab: 0.0 for tab in TABS}
    c.state["unrealized_pnls"]["Recovered"] = 0.0
    if "Recovered" not in c.state["balances"]:
        c.state["balances"]["Recovered"] = 0.0

    print("State loaded from disk.")

    # Migration: upgrade old v1 open_positions (no 'symbol' key) to Tab1
    migrated = {}
    for k, v in c.state.get("open_positions", {}).items():
        if "symbol" not in v:
            v["symbol"] = k
            v["tab"] = "Tab1"
            migrated[f"{k}_Tab1"] = v
        else:
            migrated[k] = v
    c.state["open_positions"] = migrated
    reg = c.state.setdefault("position_registry", {})
    now_iso = datetime.now(timezone.utc).isoformat()
    for pos_key, pos in c.state.get("open_positions", {}).items():
        reg.setdefault(pos_key, {
            "pos_key": pos_key,
            "tab": pos.get("tab"),
            "symbol": pos.get("symbol"),
            "side": pos.get("side"),
            "position_side": c._position_side_from_state(pos),
            "entry_price": pos.get("entry_price"),
            "qty": pos.get("qty"),
            "entry_time": pos.get("entry_time"),
            "entry_order_id": pos.get("entry_order_id"),
            "entry_client_order_id": pos.get("entry_client_order_id"),
            "status": "open",
            "created_at": pos.get("entry_time") or now_iso,
            "updated_at": now_iso,
        })

    # Backfill: ensure every tab defined in config has balance/pnl entries
    for tab in TABS:
        if tab not in c.state["balances"]:
            c.state["balances"][tab] = INITIAL_BALANCE
            print(f"[Migration] Added new tab '{tab}' with ${INITIAL_BALANCE} balance")
        if tab not in c.state["unrealized_pnls"]:
            c.state["unrealized_pnls"][tab] = 0.0

    for pos in c.state.get("open_positions", {}).values():
        if "position_side" not in pos:
            pos["position_side"] = c._position_side_name(pos.get("side"))

    if int(c.state.get("tab_stats_version", 0) or 0) < c._TAB_STATS_VERSION:
        c._rebuild_tab_stats_from_history()
    else:
        c._normalize_tab_stats()
    if int(c.state.get("symbol_stats_version", 0) or 0) < c._SYMBOL_STATS_VERSION:
        c._rebuild_symbol_stats_from_history()
    else:
        c._normalize_symbol_stats()
    c._normalize_symbol_filter_by_tab()
    c._normalize_symbol_list_by_tab("symbol_allowlist_by_tab")
    c._normalize_symbol_list_by_tab("symbol_blocklist_by_tab")

def _write_state():
    c = _core()
    """Synchronous write — called via run_in_executor to avoid blocking the event loop."""
    import tempfile
    if not c._state_write_guard_allows_save():
        return
    dir_name = os.path.dirname(c.STATE_FILE) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(c.state, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, c.STATE_FILE)
    except Exception as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        print(f"[ERROR] Failed to save c.state safely: {e}")

def _state_write_guard_allows_save():
    c = _core()
    """Prevent a freshly initialized c.state from overwriting a richer live c.state."""
    if not os.path.exists(c.STATE_FILE):
        return True
    try:
        with open(c.STATE_FILE, "r") as f:
            disk_state = json.load(f)
    except Exception:
        return True

    disk_history = len(disk_state.get("history") or [])
    next_history = len(c.state.get("history") or [])
    disk_open = len(disk_state.get("open_positions") or {})
    next_open = len(c.state.get("open_positions") or {})
    disk_used = len(disk_state.get("used_setups") or [])
    next_used = len(c.state.get("used_setups") or [])

    suspicious_history_shrink = disk_history >= 100 and next_history < max(20, int(disk_history * 0.25))
    suspicious_open_wipe = disk_open >= 20 and next_open == 0
    suspicious_setup_wipe = disk_used >= 100 and next_used == 0 and next_history < max(20, int(disk_history * 0.5))
    if not (suspicious_history_shrink or suspicious_open_wipe or suspicious_setup_wipe):
        return True

    import tempfile
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rejected_path = os.path.join(c.STATE_ARCHIVE_DIR, f"rejected_shrink_{stamp}.json")
    try:
        dir_name = os.path.dirname(c.STATE_FILE) or "."
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(c.state, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, rejected_path)
    except Exception as e:
        print(f"[CRITICAL] State shrink guard blocked save, and debug dump failed: {e}")
    print(
        "[CRITICAL] State shrink guard blocked suspicious save "
        f"(history {disk_history}->{next_history}, open {disk_open}->{next_open}, "
        f"used_setups {disk_used}->{next_used}). Rejected c.state: {rejected_path}"
    )
    return False

_state_write_lock = asyncio.Lock()

async def save_state():
    c = _core()
    async with _state_write_lock:
        await asyncio.to_thread(_write_state)
