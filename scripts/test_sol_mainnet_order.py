#!/usr/bin/env python3
"""
One-off: SOLUSDT on Binance USDT-M **mainnet** — market entry ~$10 notional,
SL/TP +/-0.25% from **fill** (hedge mode).

**Default:** no Binance STOP/TP orders — bot-managed (poll mark, then market close).
Optional: `--exchange-sl-tp` places algo SL/TP on the exchange (old behavior).

Requires in .env:
  ORDER_ENV=mainnet
  BINANCE_FUTURES_API_KEY / BINANCE_FUTURES_API_SECRET (or BINANCE_API_*)

Safety:
  Dry-run: default run (order/test only).
  Real entry + local monitor: --execute --confirm-mainnet I_ACCEPT_REAL_FUNDS_RISK

Usage (from repo root):
  .\\.venv\\Scripts\\python.exe scripts/test_sol_mainnet_order.py
  .\\.venv\\Scripts\\python.exe scripts/test_sol_mainnet_order.py --execute --confirm-mainnet I_ACCEPT_REAL_FUNDS_RISK
  .\\.venv\\Scripts\\python.exe scripts/test_sol_mainnet_order.py --execute ... --exchange-sl-tp
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import binance_live  # noqa: E402
from config import (  # noqa: E402
    BINANCE_TESTNET,
    LEVERAGE,
    ORDER_ENV,
    BINANCE_FUTURES_API_KEY,
    BINANCE_FUTURES_API_SECRET,
)

SYMBOL = "SOLUSDT"
DEFAULT_NOTIONAL = 10.0
DEFAULT_PCT = 0.0025
CONFIRM_PHRASE = "I_ACCEPT_REAL_FUNDS_RISK"
POLL_SEC = 1.0


def _algo_id(res: dict) -> int | None:
    if not res:
        return None
    raw = res.get("algoId") or res.get("clientAlgoId")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


async def _premium_mark(client: httpx.AsyncClient) -> float:
    r = await client.get(
        binance_live.BASE_URL + "/fapi/v1/premiumIndex",
        params={"symbol": SYMBOL},
    )
    r.raise_for_status()
    data = r.json()
    return float(data.get("markPrice") or 0)


def _local_exit_reason(side_long: bool, mark: float, sl_px: float, tp_px: float) -> str | None:
    """Return SL / TP / None using same side rules as server local protection."""
    if side_long:
        if mark <= sl_px:
            return "SL"
        if mark >= tp_px:
            return "TP"
    else:
        if mark >= sl_px:
            return "SL"
        if mark <= tp_px:
            return "TP"
    return None


async def _wait_local_sl_tp_and_close(
    client: httpx.AsyncClient,
    *,
    side_long: bool,
    pos_side: str,
    close_side: str,
    actual_qty: float,
    sl_px: float,
    tp_px: float,
    max_wait_sec: float,
) -> int:
    """Poll mark until SL or TP, then market-close. Ctrl+C to abort (position stays open)."""
    import time as _time_mod

    deadline = _time_mod.monotonic() + max_wait_sec
    print(
        f"Local SL/TP: monitoring mark (poll {POLL_SEC}s). SL={sl_px} TP={tp_px} "
        f"(max wait {max_wait_sec:.0f}s, Ctrl+C to stop script — position remains on exchange)"
    )
    while _time_mod.monotonic() < deadline:
        mark = await _premium_mark(client)
        reason = _local_exit_reason(side_long, mark, sl_px, tp_px)
        if reason:
            ts = datetime.now(timezone.utc).strftime("%m%d%H%M%S")
            cid = f"SOLLOC{reason}{ts}"[:36]
            print(f"[Local {reason}] mark={mark:.6f} -> market {close_side} qty={actual_qty}")
            res = await binance_live.place_market_order(
                client, SYMBOL, close_side, actual_qty, position_side=pos_side, client_order_id=cid,
            )
            print(f"Close orderId={res.get('orderId')} avgPrice={res.get('avgPrice')} executedQty={res.get('executedQty')}")
            return 0
        await asyncio.sleep(POLL_SEC)
    print("ERROR: max wait exceeded without SL/TP touch — close position manually on Binance.")
    return 1


async def main() -> int:
    p = argparse.ArgumentParser(description="Test SOLUSDT mainnet ~$10 + SL/TP +/-0.25% (local or exchange)")
    p.add_argument("--notional", type=float, default=DEFAULT_NOTIONAL, help="USD notional (default 10)")
    p.add_argument("--pct", type=float, default=DEFAULT_PCT, help="SL/TP distance as fraction (default 0.0025)")
    p.add_argument("--short", action="store_true", help="Open SHORT instead of LONG")
    p.add_argument("--execute", action="store_true", help="Send real orders (mainnet only)")
    p.add_argument("--confirm-mainnet", type=str, default="", help=f"Exact: {CONFIRM_PHRASE}")
    p.add_argument(
        "--exchange-sl-tp",
        action="store_true",
        help="Place Binance algo SL/TP (uses open-algo slots). Default: local only.",
    )
    p.add_argument(
        "--max-wait-sec",
        type=float,
        default=86400.0,
        help="Local mode: max seconds to poll for SL/TP (default 86400)",
    )
    args = p.parse_args()

    if not BINANCE_FUTURES_API_KEY or not BINANCE_FUTURES_API_SECRET:
        print("ERROR: Set BINANCE_FUTURES_API_KEY and BINANCE_FUTURES_API_SECRET in .env")
        return 2

    if args.execute:
        if ORDER_ENV != "mainnet" or BINANCE_TESTNET:
            print("ERROR: --execute requires ORDER_ENV=mainnet in .env (no testnet).")
            return 2
        if args.confirm_mainnet != CONFIRM_PHRASE:
            print(f"ERROR: For real mainnet orders pass:  --confirm-mainnet {CONFIRM_PHRASE}")
            return 2

    async with httpx.AsyncClient(timeout=45.0) as client:
        await binance_live.fetch_exchange_info(client)
        mark = await _premium_mark(client)
        if mark <= 0:
            print("ERROR: Could not read mark price")
            return 2

        qty = binance_live.round_qty(SYMBOL, args.notional / mark)
        min_q = float(binance_live.symbol_info.get(SYMBOL, {}).get("min_qty") or 0)
        min_n = float(binance_live.symbol_info.get(SYMBOL, {}).get("min_notional") or 0)
        approx_nom = qty * mark
        mode = "exchange algo SL/TP" if args.exchange_sl_tp else "local SL/TP (mark poll + market close)"
        print(f"{SYMBOL} mark={mark:.6f}  ORDER_ENV={ORDER_ENV}  BASE={binance_live.BASE_URL}")
        print(f"mode={mode}")
        print(f"notional target=${args.notional:.2f} -> qty={qty} (~${approx_nom:.2f})  LEVERAGE={LEVERAGE}")
        print(f"min_qty={min_q}  min_notional~{min_n:.2f}")
        if qty <= 0 or (min_q > 0 and qty < min_q):
            print("ERROR: qty too small for exchange rules")
            return 2
        if min_n > 0 and approx_nom + 1e-6 < min_n:
            print(f"ERROR: notional {approx_nom:.2f} below exchange minimum {min_n:.2f}")
            return 2

        side_long = not args.short
        pos_side = "LONG" if side_long else "SHORT"
        entry_side = "BUY" if side_long else "SELL"
        close_side = "SELL" if side_long else "BUY"

        if side_long:
            sl_est = mark * (1 - args.pct)
            tp_est = mark * (1 + args.pct)
        else:
            sl_est = mark * (1 + args.pct)
            tp_est = mark * (1 - args.pct)
        sl_px = binance_live.round_price(SYMBOL, sl_est)
        tp_px = binance_live.round_price(SYMBOL, tp_est)
        print(f"Hedge {pos_side}: {entry_side} market qty={qty}")
        print(f"SL ~{sl_px}  TP ~{tp_px}  (+/-{args.pct*100:.3f}% from mark; refined from avg fill after entry)")

        if not args.execute:
            try:
                await binance_live.test_market_order(client, SYMBOL, entry_side, qty, position_side=pos_side)
                print("OK: POST /fapi/v1/order/test passed (no fill).")
            except Exception as e:
                print(f"order/test failed: {e}")
                return 1
            return 0

        ts = datetime.now(timezone.utc).strftime("%m%d%H%M")
        cid_entry = f"SOLTST{ts}"[:36]

        await binance_live.set_margin_type(client, SYMBOL, "ISOLATED")
        await binance_live.set_leverage(client, SYMBOL, LEVERAGE)

        entry_res = await binance_live.place_market_order(
            client, SYMBOL, entry_side, qty, position_side=pos_side, client_order_id=cid_entry,
        )
        fill = float(entry_res.get("avgPrice") or 0) or mark
        ex_qty = float(entry_res.get("executedQty") or 0) or qty
        actual_qty = binance_live.round_qty(SYMBOL, ex_qty)
        oid = entry_res.get("orderId")
        print(f"FILLED market orderId={oid} avgPrice={fill} executedQty={actual_qty}")

        if side_long:
            sl_px = binance_live.round_price(SYMBOL, fill * (1 - args.pct))
            tp_px = binance_live.round_price(SYMBOL, fill * (1 + args.pct))
        else:
            sl_px = binance_live.round_price(SYMBOL, fill * (1 + args.pct))
            tp_px = binance_live.round_price(SYMBOL, fill * (1 - args.pct))
        print(f"Active SL={sl_px}  TP={tp_px} (from fill {fill})")

        if args.exchange_sl_tp:
            sl_oid = tp_oid = None
            try:
                sl_res = await binance_live.place_stop_loss(
                    client, SYMBOL, close_side, sl_px, actual_qty, position_side=pos_side,
                    client_algo_id=f"SOLSL{ts}"[:36],
                )
                sl_oid = _algo_id(sl_res)
                tp_res = await binance_live.place_take_profit(
                    client, SYMBOL, close_side, tp_px, actual_qty, position_side=pos_side,
                    client_algo_id=f"SOLTP{ts}"[:36],
                )
                tp_oid = _algo_id(tp_res)
            except Exception as e:
                print(f"CRITICAL: SL/TP failed: {e}")
                if sl_oid:
                    try:
                        await binance_live.cancel_algo_order(client, algo_id=sl_oid)
                    except Exception:
                        pass
                print("Attempting emergency market close...")
                try:
                    await binance_live.place_market_order(
                        client, SYMBOL, close_side, actual_qty, position_side=pos_side,
                        client_order_id=f"SOLRB{ts}"[:36],
                    )
                    print("Emergency close sent.")
                except Exception as e2:
                    print(f"Emergency close failed: {e2}")
                return 1
            print(f"SL algoId={sl_oid} @ {sl_px}  TP algoId={tp_oid} @ {tp_px}")
            print("Done (exchange SL/TP). Manage on Binance UI.")
            return 0

        return await _wait_local_sl_tp_and_close(
            client,
            side_long=side_long,
            pos_side=pos_side,
            close_side=close_side,
            actual_qty=actual_qty,
            sl_px=sl_px,
            tp_px=tp_px,
            max_wait_sec=args.max_wait_sec,
        )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
