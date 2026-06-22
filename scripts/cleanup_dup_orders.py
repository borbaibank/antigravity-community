"""One-shot: cancel every open SL/TP algo order that isn't tracked in state.

For each position in paper_state.json["open_positions"], keep only the
sl_order_id and tp_order_id that are currently recorded; cancel every other
algo order for that (symbol, positionSide). Run this after the repair-bug
placed duplicates.

Usage (from repo root):
    .\\.venv\\Scripts\\python.exe scripts/cleanup_dup_orders.py
"""
import asyncio
import json
import os
import sys

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import binance_live
from scripts._paths import STATE_FILE


def _pos_side(pos: dict) -> str:
    ps = pos.get("position_side")
    if ps:
        return str(ps).upper()
    return "LONG" if pos.get("side") == "Long" else "SHORT"


async def main():
    with open(STATE_FILE) as f:
        state = json.load(f)

    tracked: dict = {}
    for pos in state.get("open_positions", {}).values():
        sym = pos.get("symbol")
        if not sym:
            continue
        key = (sym, _pos_side(pos))
        bucket = tracked.setdefault(key, set())
        for k in ("sl_order_id", "tp_order_id"):
            v = pos.get(k)
            if v is not None:
                bucket.add(int(v))

    if not tracked:
        print("No tracked positions in state — aborting.")
        return

    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        data = await binance_live._sreq(client, "GET", "/fapi/v1/openAlgoOrders", {})
        orders = data if isinstance(data, list) else data.get("orders", [])
        print(f"Fetched {len(orders)} open algo orders.")

        cancelled = 0
        for o in orders:
            algo_id = int(o.get("algoId", 0) or 0)
            sym     = o.get("symbol", "")
            pside   = str(o.get("positionSide") or "").upper()
            key     = (sym, pside)
            if key not in tracked:
                continue  # not our position — leave alone
            if algo_id in tracked[key]:
                continue  # this is the tracked one — keep
            try:
                await binance_live.cancel_algo_order(client, algo_id=algo_id)
                print(f"  cancelled {o.get('type')} algoId={algo_id} {sym} {pside}")
                cancelled += 1
            except Exception as e:
                print(f"  FAILED {algo_id} on {sym}: {e}")

        print(f"\nDone — cancelled {cancelled} duplicate algo orders.")


if __name__ == "__main__":
    asyncio.run(main())
