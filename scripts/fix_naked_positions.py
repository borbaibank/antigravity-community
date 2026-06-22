"""
fix_naked_positions.py
======================
One-time recovery script: reads paper_state.json, checks every open live position,
and places missing STOP_MARKET / TAKE_PROFIT_MARKET orders via the algo-order
endpoint used by the bot.

Run BEFORE restarting the bot (from repo root):
    .\\.venv\\Scripts\\python.exe scripts/fix_naked_positions.py
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

PROTECTIVE_TYPES = {"STOP_MARKET", "TAKE_PROFIT_MARKET"}


def _algo_id(res: dict) -> int:
    return int(res.get("algoId") or res.get("orderId") or 0)


def _algo_type(order: dict) -> str:
    return str(order.get("orderType") or order.get("type") or "").upper()


def _trigger_price(order: dict) -> float:
    return float(order.get("triggerPrice") or order.get("stopPrice") or 0)


def _same_price(a: float, b: float) -> bool:
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) <= max(abs(b) * 0.00001, 1e-12)


async def main():
    async with httpx.AsyncClient(timeout=15) as client:
        # 1. Load exchange info (for rounding)
        print("Loading exchange info...")
        await binance_live.fetch_exchange_info(client)

        # 2. Read state
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)

        open_pos = state.get("open_positions", {})
        if not open_pos:
            print("No open positions found in state. Nothing to do.")
            return

        print(f"\nFound {len(open_pos)} open position(s):\n")

        # 3. Fetch all current open algo orders from Binance (one call)
        print("Fetching open algo orders from Binance...")
        try:
            all_orders = await binance_live.get_algo_open_orders(client)
        except Exception as e:
            print(f"[ERROR] Could not fetch open algo orders: {e}")
            all_orders = []

        alive_by_id = {
            int(o["algoId"]): o
            for o in all_orders
            if o.get("algoId") and _algo_type(o) in PROTECTIVE_TYPES
        }
        consumed_ids = set()
        for pos in open_pos.values():
            for k in ("sl_order_id", "tp_order_id"):
                oid = pos.get(k)
                if oid and int(oid) in alive_by_id:
                    consumed_ids.add(int(oid))

        state_changed = False

        def _find_unclaimed(sym: str, pos_side: str, order_type: str, target_px: float) -> dict | None:
            for order in all_orders:
                algo_id = int(order.get("algoId", 0) or 0)
                if not algo_id or algo_id in consumed_ids:
                    continue
                if order.get("symbol") != sym:
                    continue
                if str(order.get("positionSide") or "").upper() != pos_side:
                    continue
                if _algo_type(order) != order_type:
                    continue
                if _same_price(_trigger_price(order), target_px):
                    consumed_ids.add(algo_id)
                    return order
            return None

        for pos_key, pos in list(open_pos.items()):
            sym  = pos["symbol"]
            side = pos["side"]          # "Long" / "Short"
            sl   = float(pos.get("sl", 0))
            tp   = float(pos.get("tp", 0))
            qty  = float(pos.get("qty", 0))
            pos_side   = "LONG" if side == "Long" else "SHORT"
            close_side = "SELL" if side == "Long" else "BUY"

            if sl == 0 or tp == 0 or qty == 0:
                print(f"  ⚠  {pos_key}: SL={sl} TP={tp} qty={qty} — skipping (zero values)")
                continue

            print(f"\n  Position: {pos_key}")
            print(f"    Side={side}  SL={sl:.4f}  TP={tp:.4f}  qty={qty}")

            # ── SL ────────────────────────────────────────────────────────────
            sl_covered = False
            stored_sl_oid = pos.get("sl_order_id")
            if stored_sl_oid and int(stored_sl_oid) in alive_by_id:
                order = alive_by_id[int(stored_sl_oid)]
                sl_covered = (
                    order.get("symbol") == sym
                    and str(order.get("positionSide") or "").upper() == pos_side
                    and _algo_type(order) == "STOP_MARKET"
                )
                if sl_covered:
                    print(f"    ✅ SL algo #{stored_sl_oid} already active on Binance")
            if not sl_covered:
                adopted = _find_unclaimed(sym, pos_side, "STOP_MARKET", sl)
                if adopted:
                    new_oid = int(adopted["algoId"])
                    pos["sl_order_id"] = new_oid
                    sl_covered = True
                    state_changed = True
                    print(f"    ✅ SL adopted: algo #{new_oid} @ {_trigger_price(adopted):.4f}")

            if not sl_covered:
                try:
                    res = await binance_live.place_stop_loss(
                        client, sym, close_side, sl, qty, position_side=pos_side
                    )
                    new_oid = _algo_id(res)
                    pos["sl_order_id"] = new_oid
                    state_changed = True
                    print(f"    🛡  SL placed: #{new_oid} @ {sl:.4f}")
                except Exception as e:
                    print(f"    ❌ SL FAILED: {e}")
            else:
                print(f"    ✅ SL already covered")

            # ── TP ────────────────────────────────────────────────────────────
            tp_covered = False
            stored_tp_oid = pos.get("tp_order_id")
            if stored_tp_oid and int(stored_tp_oid) in alive_by_id:
                order = alive_by_id[int(stored_tp_oid)]
                tp_covered = (
                    order.get("symbol") == sym
                    and str(order.get("positionSide") or "").upper() == pos_side
                    and _algo_type(order) == "TAKE_PROFIT_MARKET"
                )
                if tp_covered:
                    print(f"    ✅ TP algo #{stored_tp_oid} already active on Binance")
            if not tp_covered:
                adopted = _find_unclaimed(sym, pos_side, "TAKE_PROFIT_MARKET", tp)
                if adopted:
                    new_oid = int(adopted["algoId"])
                    pos["tp_order_id"] = new_oid
                    tp_covered = True
                    state_changed = True
                    print(f"    ✅ TP adopted: algo #{new_oid} @ {_trigger_price(adopted):.4f}")

            if not tp_covered:
                try:
                    res = await binance_live.place_take_profit(
                        client, sym, close_side, tp, qty, position_side=pos_side
                    )
                    new_oid = _algo_id(res)
                    pos["tp_order_id"] = new_oid
                    state_changed = True
                    print(f"    🎯 TP placed: #{new_oid} @ {tp:.4f}")
                except Exception as e:
                    print(f"    ❌ TP FAILED: {e}")
            else:
                print(f"    ✅ TP already covered")

        # 4. Save updated state
        if state_changed:
            backup = STATE_FILE + ".bak"
            import shutil
            shutil.copy2(STATE_FILE, backup)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            print(f"\n✅ State saved. Backup at: {backup}")
        else:
            print("\nNo changes needed — all positions already have SL/TP.")

        print("\nDone. Safe to restart the bot.\n")


if __name__ == "__main__":
    asyncio.run(main())
