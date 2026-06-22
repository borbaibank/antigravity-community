"""Dry-run signal scan — fetch klines + evaluate signals, no orders.

Usage:
  .\\.venv\\Scripts\\python.exe tools\\dry_scan.py
  .\\.venv\\Scripts\\python.exe tools\\dry_scan.py --interval 1h
  .\\.venv\\Scripts\\python.exe tools\\dry_scan.py --interval 4h --max-symbols 20
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run candle signal scan (no orders)")
    parser.add_argument("--interval", choices=("1h", "4h"), default="1h")
    parser.add_argument(
        "--max-symbols",
        type=int,
        default=0,
        help="Limit symbols for a quick test (0 = use bot scan limit)",
    )
    args = parser.parse_args()

    import bot.core as core
    from bot.engine import entry as entry_mod
    from bot.feeds.market import fetch_scan_symbols

    core._http_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    entry_mod._scan_gate_open = lambda interval, now=None: True  # bypass startup gate

    await fetch_scan_symbols()
    if not core.SCAN_SYMBOLS:
        print("[Error] SCAN_SYMBOLS empty — cannot scan")
        await core._http_client.aclose()
        core._http_client = None
        return 1

    orig_symbols = core._symbols_for_interval_scan

    def limited_symbols(interval: str):
        symbols = orig_symbols(interval)
        if args.max_symbols > 0:
            return symbols[: args.max_symbols]
        return symbols

    core._symbols_for_interval_scan = limited_symbols

    print("=" * 56)
    print(f"  DRY SCAN  interval={args.interval}  LIVE_MODE={core.LIVE_MODE}")
    print(f"  order={core.ORDER_ENV}  feed={core.PRICE_FEED_ENV}")
    enabled = [t for t in core.TABS if core.state.get("tab_enabled", {}).get(t)]
    print(f"  enabled tabs: {', '.join(enabled) or '(none)'}")
    print("=" * 56)

    try:
        payload = await entry_mod.scan_candle_signals(args.interval)
    finally:
        await core._http_client.aclose()
        core._http_client = None

    if payload is None:
        print("\n[Result] Scan returned None (gate, ban, or no enabled tabs on TF)")
        return 1

    by_tab = payload.get("candidates_by_tab") or {}
    total = sum(len(v) for v in by_tab.values())
    print(f"\n[Result] OK — {total} candidate(s)")
    for tab in core.TABS:
        items = by_tab.get(tab) or []
        if not items:
            continue
        print(f"  {tab}: {len(items)}")
        for item in items[:5]:
            sig = item.get("sig") or {}
            side = sig.get("side", "?")
            ep = sig.get("ep", "?")
            print(f"    - {item['sym']} {side} ep={ep}")
        if len(items) > 5:
            print(f"    ... +{len(items) - 5} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
