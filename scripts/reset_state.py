"""Reset paper_state.json to fresh defaults (backup existing file first).

Usage (from repo root):
    .\\.venv\\Scripts\\python.exe scripts/reset_state.py
"""
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config import (
    INITIAL_BALANCE,
    LEVERAGE,
    MAX_POSITIONS_PER_TAB,
    NOTIONAL_SIZE,
    SLTP_MODE,
    STARTUP_ENABLED_TABS,
    SYMBOL_SCAN_LIMIT,
    TABS,
)

from scripts._paths import STATE_FILE, STATE_ARCHIVE_DIR, ensure_archive_dirs


def _startup_tab_enabled() -> dict[str, bool]:
    return {tab: tab in set(STARTUP_ENABLED_TABS) for tab in TABS}


def fresh_state() -> dict:
    state = {
        "balances": {tab: INITIAL_BALANCE for tab in TABS},
        "unrealized_pnls": {tab: 0.0 for tab in TABS},
        "open_positions": {},
        "pending_entry_orders": {},
        "position_registry": {},
        "history": [],
        "used_setups": [],
        "sync_issues": [],
        "error_events": [],
        "margin_history": [],
        "prescreen_watchlists": {},
        "binance_income": {
            "realized_pnl": 0.0,
            "commission": 0.0,
            "funding": 0.0,
            "last_ts": int(datetime.now(timezone.utc).timestamp() * 1000),
            "seed_ts": int(datetime.now(timezone.utc).timestamp() * 1000),
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "seen_tran_ids": [],
            "gross_rebuilt": False,
        },
        "binance_tab_income": {tab: {"gross_profit": 0.0, "gross_loss": 0.0} for tab in TABS},
        "tab_stats": {},
        "tab_stats_version": 0,
        "tab_enabled": _startup_tab_enabled(),
        "long_short_balance_mode": "off",
        "trade_side_mode": "both",
        "max_positions_per_tab": MAX_POSITIONS_PER_TAB,
        "margin_size": NOTIONAL_SIZE / LEVERAGE if LEVERAGE > 0 else NOTIONAL_SIZE,
        "leverage": LEVERAGE,
        "notional_size": NOTIONAL_SIZE,
        "symbol_scan_limit": SYMBOL_SCAN_LIMIT,
        "symbol_scan_limit_by_tab": {"Tab17": 500},
        "sltp_mode": SLTP_MODE,
        "circuit_breaker": False,
        "daily_loss_usd": 0.0,
        "daily_loss_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    state["balances"]["Recovered"] = 0.0
    state["balances"]["SafeGuard"] = 0.0
    state["unrealized_pnls"]["Recovered"] = 0.0
    state["unrealized_pnls"]["SafeGuard"] = 0.0
    for extra in ("Recovered", "SafeGuard"):
        state["binance_tab_income"][extra] = {"gross_profit": 0.0, "gross_loss": 0.0}
    return state


def main() -> int:
    ensure_archive_dirs()
    if os.path.exists(STATE_FILE):
        backup = os.path.join(STATE_ARCHIVE_DIR, f"paper_state.backup.{int(time.time())}.json")
        shutil.copy2(STATE_FILE, backup)
        print(f"Backed up to {backup}")
    else:
        print("No existing state file — creating fresh state.")

    state = fresh_state()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

    enabled = [tab for tab, on in state["tab_enabled"].items() if on]
    print(
        f"Reset {STATE_FILE}: ${INITIAL_BALANCE:.0f}/tab, "
        f"0 open positions, 0 history, notional={NOTIONAL_SIZE}, "
        f"max_positions={MAX_POSITIONS_PER_TAB}"
    )
    print(f"Enabled tabs: {', '.join(enabled)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
