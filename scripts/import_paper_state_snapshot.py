"""Import Tab18 history from a paper_state JSON snapshot.

Accepts a full paper_state dict or {"history": [...]} export.

Usage (from repo root):
    python scripts/import_paper_state_snapshot.py --dry-run
    python scripts/import_paper_state_snapshot.py --apply
    python scripts/import_paper_state_snapshot.py --source path/to/file.json --apply
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config import HISTORY_CAP, INITIAL_BALANCE, TABS
from scripts._paths import STATE_ARCHIVE_DIR, STATE_FILE, ensure_archive_dirs
from scripts.import_tab18_backtest_history import (
    TAB,
    atomic_save_state,
    load_or_create_state,
    strip_tab18,
)

DEFAULT_SOURCE = os.path.join(ROOT, "tab18_paper_state_mar2025_full.json")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Import Tab18 paper_state JSON snapshot")
    p.add_argument("--source", default=DEFAULT_SOURCE, help="Snapshot JSON path")
    p.add_argument(
        "--full-replace",
        action="store_true",
        help="Replace entire paper_state.json with snapshot (must be full state dict)",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", action="store_true")
    return p.parse_args()


def load_snapshot(path: str) -> dict | list:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_history(payload) -> list[dict]:
    if isinstance(payload, list):
        return list(payload)
    if isinstance(payload, dict) and isinstance(payload.get("history"), list):
        return list(payload["history"])
    raise ValueError("Snapshot must be a paper_state dict with 'history' or a history list")


def summarize_history(rows: list[dict]) -> dict:
    tabs: dict[str, int] = {}
    for row in rows:
        tab = row.get("tab") or "Recovered"
        tabs[tab] = tabs.get(tab, 0) + 1
    exit_times = sorted(r.get("exit_time") for r in rows if r.get("exit_time"))
    total_pnl = sum(float(r.get("pnl_usd") or 0) for r in rows)
    return {
        "count": len(rows),
        "tabs": tabs,
        "first_exit": exit_times[0] if exit_times else None,
        "last_exit": exit_times[-1] if exit_times else None,
        "total_pnl": total_pnl,
    }


def apply_full_replace(snapshot: dict) -> dict:
    if "history" not in snapshot:
        raise ValueError("Full replace requires a paper_state dict with 'history'")
    state = dict(snapshot)
    if len(state.get("history") or []) > HISTORY_CAP:
        state["history"] = sorted(
            state["history"], key=lambda h: h.get("exit_time") or ""
        )[-HISTORY_CAP:]
    state["tab_stats_version"] = 0
    state["symbol_stats_version"] = 0
    return state


def merge_tab18_snapshot(state: dict, snapshot: dict, rows: list[dict]) -> None:
    tab18_rows = [r for r in rows if (r.get("tab") or "Recovered") == TAB]
    if not tab18_rows:
        raise ValueError(f"No {TAB} rows found in snapshot")

    tab18_rows.sort(key=lambda r: r.get("exit_time") or "")
    if len(tab18_rows) > HISTORY_CAP:
        tab18_rows = tab18_rows[-HISTORY_CAP:]

    strip_tab18(state)
    other = [h for h in (state.get("history") or []) if h.get("tab") != TAB]
    merged = other + tab18_rows
    if len(merged) > HISTORY_CAP:
        merged = sorted(merged, key=lambda h: h.get("exit_time") or "")[-HISTORY_CAP:]
    state["history"] = merged

    tab18_pnl = sum(float(r.get("pnl_usd") or 0) for r in tab18_rows)
    src_bal = snapshot.get("balances", {}).get(TAB)
    if src_bal is not None:
        state.setdefault("balances", {})[TAB] = float(src_bal)
    else:
        state.setdefault("balances", {})[TAB] = INITIAL_BALANCE + tab18_pnl
    state.setdefault("unrealized_pnls", {})[TAB] = 0.0
    state["tab_stats_version"] = 0
    state["symbol_stats_version"] = 0


def main() -> int:
    args = parse_args()
    if args.dry_run == args.apply:
        print("Specify exactly one of --dry-run or --apply")
        return 1
    if TAB not in TABS:
        print(f"{TAB} is not configured in TABS")
        return 1
    if not os.path.isfile(args.source):
        print(f"Snapshot not found: {args.source}")
        print("Copy tab18_paper_state_mar2025_full.json into the repo root or pass --source")
        return 1

    payload = load_snapshot(args.source)
    rows = extract_history(payload)
    summary = summarize_history(rows)

    print("=== paper_state snapshot import ===")
    print(f"Source: {args.source}")
    print(f"Mode:   {'full-replace' if args.full_replace else 'merge Tab18 history'}")
    print(f"Rows:   {summary['count']:,}")
    print(f"Tabs:   {summary['tabs']}")
    if summary["first_exit"]:
        print(f"Exit:   {summary['first_exit']} -> {summary['last_exit']}")
    print(f"PnL (all rows): ${summary['total_pnl']:,.2f}")
    print(f"HISTORY_CAP: {HISTORY_CAP:,}")

    tab18_count = summary["tabs"].get(TAB, 0)
    if tab18_count > HISTORY_CAP:
        print(f"Note: {TAB} rows ({tab18_count:,}) exceed cap — will keep newest {HISTORY_CAP:,}")

    if args.dry_run:
        return 0

    ensure_archive_dirs()
    if os.path.isfile(STATE_FILE):
        backup = os.path.join(
            STATE_ARCHIVE_DIR,
            f"paper_state.pre_snapshot_import.{int(time.time())}.json",
        )
        shutil.copy2(STATE_FILE, backup)
        print(f"Backup: {backup}")
    else:
        print("No existing paper_state.json — starting from fresh defaults.")

    if args.full_replace:
        if not isinstance(payload, dict):
            print("Full replace requires a paper_state dict")
            return 1
        state = apply_full_replace(payload)
    else:
        state = load_or_create_state()
        snap_dict = payload if isinstance(payload, dict) else {"history": rows}
        merge_tab18_snapshot(state, snap_dict, rows)

    atomic_save_state(state)

    print(f"Wrote {STATE_FILE} ({len(state.get('history') or []):,} history rows)")
    print(f"Tab18 balance: ${float((state.get('balances') or {}).get(TAB, 0)):,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
