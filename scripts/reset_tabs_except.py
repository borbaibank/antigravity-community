"""Reset all tabs except a keep-list (balance, positions, history, setups, registry).

Usage (from repo root):
    .\\.venv\\Scripts\\python.exe scripts/reset_tabs_except.py Tab11 Tab18
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config import INITIAL_BALANCE, TABS
from scripts._paths import STATE_FILE, STATE_ARCHIVE_DIR, ensure_archive_dirs


def reset_tabs_except(keep: set[str]) -> int:
    invalid = keep - set(TABS)
    if invalid:
        print(f"Invalid tab(s): {', '.join(sorted(invalid))}")
        return 1

    reset = [t for t in TABS if t not in keep]
    if not reset:
        print("Nothing to reset — keep-list covers all tabs.")
        return 0

    if not os.path.exists(STATE_FILE):
        print("No paper_state.json found.")
        return 1

    ensure_archive_dirs()
    backup = os.path.join(STATE_ARCHIVE_DIR, f"paper_state.backup.{int(time.time())}.json")
    shutil.copy2(STATE_FILE, backup)
    print(f"Backup: {backup}")

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    for tab in reset:
        state.setdefault("balances", {})[tab] = INITIAL_BALANCE
        state.setdefault("unrealized_pnls", {})[tab] = 0.0
        state.setdefault("binance_tab_income", {})[tab] = {
            "gross_profit": 0.0,
            "gross_loss": 0.0,
        }

        state["open_positions"] = {
            k: v
            for k, v in state.get("open_positions", {}).items()
            if v.get("tab") != tab
        }
        state["history"] = [h for h in state.get("history", []) if h.get("tab") != tab]

        needle = f"_{tab}_"
        state["used_setups"] = [
            u for u in state.get("used_setups", []) if needle not in str(u)
        ]

        suffix = f"_{tab}"
        reg = state.get("position_registry") or {}
        state["position_registry"] = {
            k: v
            for k, v in reg.items()
            if not (k.endswith(suffix) or v.get("tab") == tab)
        }

        tab_stats = state.get("tab_stats") or {}
        tab_stats.pop(tab, None)
        state["tab_stats"] = tab_stats

        symbol_filter = state.get("symbol_filter_by_tab") or {}
        symbol_filter.pop(tab, None)
        state["symbol_filter_by_tab"] = symbol_filter

        state.setdefault("tab_enabled", {})[tab] = False

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

    print(f"Reset {len(reset)} tab(s): {', '.join(reset)}")
    print(f"Kept: {', '.join(sorted(keep))}")
    enabled = [t for t, on in state.get("tab_enabled", {}).items() if on]
    print(f"Enabled tabs now: {', '.join(enabled) or '(none)'}")
    return 0


def main() -> int:
    keep = set(sys.argv[1:]) if len(sys.argv) > 1 else {"Tab11", "Tab18"}
    return reset_tabs_except(keep)


if __name__ == "__main__":
    sys.exit(main())
