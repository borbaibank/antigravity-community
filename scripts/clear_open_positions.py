"""Clear open_positions from paper_state.json without wiping history or balances.

Usage (from repo root):
    .\\.venv\\Scripts\\python.exe scripts/clear_open_positions.py
"""
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config import TABS
from scripts._paths import STATE_FILE, STATE_ARCHIVE_DIR, ensure_archive_dirs


def clear_open_positions(state: dict, reason: str = "manual_state_clear") -> dict:
    cleared = len(state.get("open_positions") or {})
    now = datetime.now(timezone.utc).isoformat()

    state["open_positions"] = {}

    unrealized = state.setdefault("unrealized_pnls", {})
    for tab in TABS:
        unrealized[tab] = 0.0
    unrealized["Recovered"] = 0.0
    unrealized["SafeGuard"] = 0.0

    registry = state.setdefault("position_registry", {})
    closed_registry = 0
    for pos_key, entry in registry.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("status") == "closed":
            continue
        entry["status"] = "closed"
        entry["closed_reason"] = reason
        entry["closed_at"] = now
        entry["updated_at"] = now
        closed_registry += 1

    return {
        "cleared_open_positions": cleared,
        "closed_registry_entries": closed_registry,
        "history_kept": len(state.get("history") or []),
    }


def main() -> int:
    if not os.path.exists(STATE_FILE):
        print(f"No {STATE_FILE} found.")
        return 1

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    ensure_archive_dirs()
    backup = os.path.join(STATE_ARCHIVE_DIR, f"paper_state.backup.{int(time.time())}.json")
    shutil.copy2(STATE_FILE, backup)
    print(f"Backed up to {backup}")

    summary = clear_open_positions(state)

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=4)

    print(
        f"Cleared {summary['cleared_open_positions']} open position(s); "
        f"marked {summary['closed_registry_entries']} registry row(s) closed; "
        f"kept {summary['history_kept']} history row(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
