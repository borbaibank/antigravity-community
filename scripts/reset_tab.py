"""
Reset a single Tab's balance, open positions, and history.

Usage (from repo root):
    .\\.venv\\Scripts\\python.exe scripts/reset_tab.py Tab1
    .\\.venv\\Scripts\\python.exe scripts/reset_tab.py Tab3
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config import TABS, INITIAL_BALANCE
from scripts._paths import STATE_FILE

def reset_tab(tab: str):
    if tab not in TABS:
        print(f"Invalid tab '{tab}'. Choose from: {', '.join(TABS)}")
        sys.exit(1)

    if not os.path.exists(STATE_FILE):
        print("No paper_state.json found. Nothing to reset.")
        sys.exit(0)

    with open(STATE_FILE, "r") as f:
        state = json.load(f)

    state.setdefault("balances", {})[tab] = INITIAL_BALANCE
    state.setdefault("unrealized_pnls", {})[tab] = 0.0

    before = len(state.get("open_positions", {}))
    state["open_positions"] = {
        k: v for k, v in state.get("open_positions", {}).items()
        if v.get("tab") != tab
    }
    closed_positions = before - len(state["open_positions"])

    before_h = len(state.get("history", []))
    state["history"] = [h for h in state.get("history", []) if h.get("tab") != tab]
    cleared_history = before_h - len(state["history"])

    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=4)

    print(f"{tab} reset — balance: ${INITIAL_BALANCE:,.2f}, "
          f"positions closed: {closed_positions}, history cleared: {cleared_history}")

if __name__ == "__main__":
    tab_arg = sys.argv[1] if len(sys.argv) > 1 else "Tab1"
    reset_tab(tab_arg)
