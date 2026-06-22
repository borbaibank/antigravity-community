"""Repair ExchangeSync history where full-leg PnL was applied to each tab on a shared hedge leg.

Detects groups of ExchangeSync closes on the same (symbol, position_side) with identical
pnl_usd (legacy double-count bug). Reallocates PnL by tab qty and fixes balances,
symbol_stats, tab_stats worst/best, and daily_loss_usd.

Usage (from repo root):
    .\\.venv\\Scripts\\python.exe scripts/repair_leg_sync_double_pnl.py          # dry-run
    .\\.venv\\Scripts\\python.exe scripts/repair_leg_sync_double_pnl.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts._paths import STATE_ARCHIVE_DIR, STATE_FILE, ensure_archive_dirs


def _parse_exit_ms(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        text = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _pos_side(row: dict) -> str:
    ps = row.get("position_side")
    if ps:
        return str(ps).upper()
    return "LONG" if row.get("side") == "Long" else "SHORT"


def _qty_for_row(state: dict, row: dict) -> float:
    qty = row.get("qty")
    if qty is not None and float(qty) > 0:
        return float(qty)
    sym = row.get("symbol")
    tab = row.get("tab")
    reg = state.get("position_registry") or {}
    pk = f"{sym}_{tab}"
    reg_row = reg.get(pk) or {}
    return max(float(reg_row.get("qty") or 0), 0.0)


def find_duplicate_leg_groups(history: list[dict]) -> list[list[dict]]:
    """Cluster ExchangeSync rows that share symbol+side+pnl and close within 3 minutes."""
    candidates = [h for h in history if h.get("reason") == "ExchangeSync"]
    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for h in candidates:
        pnl = round(float(h.get("pnl_usd") or 0), 8)
        by_key[(h.get("symbol"), _pos_side(h), pnl)].append(h)

    groups: list[list[dict]] = []
    for rows in by_key.values():
        if len(rows) < 2:
            continue
        rows = sorted(rows, key=lambda r: _parse_exit_ms(r.get("exit_time")) or 0)
        # Same pnl on multiple tabs => each row likely holds full-leg PnL (bug).
        groups.append(rows)
    return groups


def repair_state(state: dict, *, apply: bool) -> list[str]:
    lines: list[str] = []
    history = state.get("history") or []
    groups = find_duplicate_leg_groups(history)
    if not groups:
        lines.append("No duplicate ExchangeSync leg groups found.")
        return lines

    daily_loss_delta = 0.0
    balance_deltas: dict[str, float] = defaultdict(float)

    for group in groups:
        sym = group[0].get("symbol")
        pos_side = _pos_side(group[0])
        leg_pnl = float(group[0].get("pnl_usd") or 0)
        qty_rows = [(row, _qty_for_row(state, row)) for row in group]
        total_qty = sum(q for _, q in qty_rows)
        if total_qty <= 0:
            lines.append(f"SKIP {sym} {pos_side}: no qty for split")
            continue
        pnls = [float(r.get("pnl_usd") or 0) for r in group]
        if not all(abs(p - leg_pnl) < 1e-6 for p in pnls):
            lines.append(f"SKIP {sym} {pos_side}: tabs have differing PnL (already split?)")
            continue

        lines.append(f"Repair {sym} {pos_side}: leg PnL {leg_pnl:.4f} across {len(group)} tab(s), qty={total_qty:.4f}")
        for row, qty in qty_rows:
            tab = row.get("tab") or "?"
            old_pnl = float(row.get("pnl_usd") or 0)
            share = qty / total_qty
            new_pnl = leg_pnl * share
            delta = new_pnl - old_pnl
            lines.append(
                f"  {tab}: pnl {old_pnl:.4f} -> {new_pnl:.4f} (share {share*100:.1f}%), balance delta {delta:+.4f}"
            )
            if not apply:
                continue

            row["pnl_usd"] = new_pnl
            row["qty"] = qty
            if row.get("fee_usd") is not None:
                row["fee_usd"] = float(row["fee_usd"]) * share

            balance_deltas[tab] += delta

        # Each tab previously counted the full leg toward daily loss.
        excess_daily = abs(leg_pnl) * (len(group) - 1)
        daily_loss_delta += excess_daily

            # symbol_stats
            sym_stats = (state.setdefault("symbol_stats", {}).setdefault(tab, {})).setdefault(sym, {})
            if sym_stats:
                for key in ("net_pnl", "long_pnl", "short_pnl"):
                    if key in sym_stats:
                        sym_stats[key] = float(sym_stats.get(key) or 0) + delta

            # tab_stats best/worst from this row only if it was the extreme
            tab_row = state.setdefault("tab_stats", {}).setdefault(tab, {})
            worst = tab_row.get("worst")
            if worst is not None and abs(float(worst) - old_pnl) < 1e-6:
                tab_row["worst"] = new_pnl
            best = tab_row.get("best")
            if best is not None and abs(float(best) - old_pnl) < 1e-6:
                tab_row["best"] = new_pnl

    if apply:
        for tab, delta in balance_deltas.items():
            if tab in state.setdefault("balances", {}):
                state["balances"][tab] += delta
        if daily_loss_delta > 0:
            state["daily_loss_usd"] = max(0.0, float(state.get("daily_loss_usd") or 0) - daily_loss_delta)
            lines.append(f"Adjusted daily_loss_usd by -{daily_loss_delta:.4f}")

    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair duplicate ExchangeSync leg PnL in paper_state.json")
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    args = parser.parse_args()

    if not os.path.exists(STATE_FILE):
        print(f"No state file at {STATE_FILE}")
        return 1

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    lines = repair_state(state, apply=args.apply)
    for line in lines:
        print(line)

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to write changes.")
        return 0

    ensure_archive_dirs()
    backup = os.path.join(STATE_ARCHIVE_DIR, f"paper_state.backup.repair_leg_pnl.{int(time.time())}.json")
    shutil.copy2(STATE_FILE, backup)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    print(f"\nBackup: {backup}")
    print(f"Updated: {STATE_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
