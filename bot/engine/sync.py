"""Live position sync, UDS, and order update handlers (extracted from bot.core)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pandas as pd
import strategies
import binance_live
import websockets
from config import (
    EXCHANGE_ACCOUNT_POLL_SEC_NO_UDS,
    EXCHANGE_ACCOUNT_POLL_SEC_UDS,
    EXCHANGE_ACCOUNT_SYNC_SEC_NO_UDS,
    EXCHANGE_ACCOUNT_SYNC_SEC_UDS,
    EXCHANGE_ACCOUNT_SYNC_SEC_UDS_CONNECTED,
    UDS_ACCOUNT_FRESH_SEC,
)

def _core():
    from bot import core
    return core


async def handle_order_update(order: dict):
    c = _core()
    """Handle ORDER_TRADE_UPDATE from Binance User Data Stream."""
    status = str(order.get("X") or "").upper()
    order_type = str(order.get("o") or order.get("ot") or "").upper()
    client_id = c._order_client_id(order)

    if order_type == "LIMIT" and c._strategy_role_from_client_id(client_id) == "ENTRY":
        if status in ("PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED", "REJECTED"):
            from bot.engine.entry import handle_entry_limit_order_update
            await handle_entry_limit_order_update(order, status)
        return

    if status != "FILLED":
        return

    order_id       = int(order["i"])
    sym            = order["s"]
    fill_price     = float(order.get("ap") or order.get("L") or 0)
    fill_qty       = float(order.get("z") or order.get("q") or 0)
    event_pos_side = str(order.get("ps") or "").upper()
    realized_pnl   = float(order["rp"]) if order.get("rp") not in (None, "", "0", 0) else None
    fill_commission, fill_fee_usd = c._order_commission_parts(order)

    is_sl_type     = order_type in ("STOP_MARKET", "STOP")
    is_tp_type     = order_type in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT")
    is_reduce_only = order.get("R") is True
    close_side = order.get("S", "")

    # Bot-initiated market closes are reconciled by c._close_position_unsafe()
    # after a fresh position-risk verification. Letting this fill event guess
    # again can remove a sibling strategy on the same symbol/side.
    close_role = c._strategy_role_from_client_id(client_id)
    if order_type in ("MARKET", "LIMIT") and close_role in {"CLOSE", "CLOSEPM"}:
        return
    if order_type in ("MARKET", "LIMIT") and c._consume_recent_bot_close_fill(sym, event_pos_side, close_side, fill_qty):
        print(f"[Live] ignored bot-initiated close fill echo for {sym} {event_pos_side} qty={fill_qty}")
        return

    # Manual close detection: any MARKET/LIMIT fill whose side opposes an open
    # position on the same (symbol, positionSide) should be treated as a close,
    # even without reduceOnly (Binance UI does not always set R=true).
    is_manual_type = False
    if order_type in ("MARKET", "LIMIT"):
        if is_reduce_only:
            is_manual_type = True
        else:
            expected_close_side_for_long  = "SELL"
            expected_close_side_for_short = "BUY"
            for p in c.state["open_positions"].values():
                if p.get("symbol") != sym:
                    continue
                if event_pos_side and c._position_side_from_state(p) != event_pos_side:
                    continue
                if (p["side"] == "Long"  and close_side == expected_close_side_for_long) or \
                   (p["side"] == "Short" and close_side == expected_close_side_for_short):
                    is_manual_type = True
                    break

    if not (is_sl_type or is_tp_type or is_manual_type):
        return   # not a closing fill

    async with c._state_lock:
        # Gather all candidate positions matching symbol + positionSide + close_side
        candidates = [
            (pk, p) for pk, p in c.state["open_positions"].items()
            if p.get("symbol") == sym
            and (not event_pos_side or c._position_side_from_state(p) == event_pos_side)
            and ("SELL" if p["side"] == "Long" else "BUY") == close_side
        ]
        if not candidates:
            return

        if fill_price <= 0:
            # For STOP/TP market orders, Binance fills at market so use L (last price).
            # sp is only the *trigger* price, not the actual execution price — avoid using it
            # as fill price; only fall back to it if nothing else is available.
            fill_price = float(order.get("L") or order.get("ap") or order.get("sp") or 0)

        # ── Identify WHICH position was closed ────────────────────────────
        matched_key: str | None = None

        # 1. Exact orderId match (works when SL/TP are regular orders)
        for pk, p in candidates:
            sl_oid = p.get("sl_order_id")
            tp_oid = p.get("tp_order_id")
            if (is_sl_type and order_id == sl_oid) or (is_tp_type and order_id == tp_oid):
                matched_key = pk
                break

        # 2. Only one candidate → unambiguous unless a sibling was just bot-closed
        # on the same hedge leg (target already removed from c.state before fill echo).
        if matched_key is None and len(candidates) == 1:
            if is_manual_type and c._has_recent_bot_close_on_leg(sym, event_pos_side, close_side):
                print(
                    f"[Live] ignored close fill echo on {sym} {event_pos_side}; "
                    f"bot closed sibling on shared leg"
                )
                return
            matched_key = candidates[0][0]

        open_algo_ids: set[int] | None = None
        inferred_reason: str | None = None

        async def _open_algo_ids() -> set[int]:
            nonlocal open_algo_ids
            if open_algo_ids is None:
                try:
                    open_algo_ids = await binance_live.fetch_open_algo_order_ids(c._http_client, sym)
                except Exception as e:
                    print(f"[Live] fill detection algo-query failed {sym}: {e}")
                    open_algo_ids = set()
            return open_algo_ids

        # 3. Multiple candidates: vanished exchange SL/TP algo, or orderId match
        if matched_key is None:
            ids = await _open_algo_ids()
            if is_sl_type or is_tp_type:
                for pk, p in candidates:
                    sl_oid = p.get("sl_order_id")
                    tp_oid = p.get("tp_order_id")
                    check_id = sl_oid if is_sl_type else tp_oid
                    if check_id and int(check_id) not in ids:
                        matched_key = pk
                        break
            else:
                manual_hits = []
                for pk, p in candidates:
                    hit = c._vanished_exchange_protection_reason(p, ids)
                    if hit:
                        manual_hits.append((pk, hit))
                if len(manual_hits) == 1:
                    matched_key, inferred_reason = manual_hits[0]

        # 4. Manual close with multiple tabs: exact fill qty match when unique.
        if matched_key is None and is_manual_type and len(candidates) > 1 and fill_qty > 0:
            qty_hits = c._exact_qty_match_candidates(candidates, fill_qty, sym)
            if len(qty_hits) == 1:
                matched_key = qty_hits[0][0]
                print(
                    f"[Live] manual close qty-match {matched_key} "
                    f"(fill_qty={fill_qty})"
                )

        # 5. Fallback: defer when still ambiguous. A bot/manual close echo may not
        # include client id, and same-symbol strategy legs can have identical quantities.
        if matched_key is None and is_manual_type and len(candidates) > 1:
            print(f"[Live] ambiguous manual close fill on {sym} {event_pos_side}; deferring to sync")
            asyncio.create_task(c.record_sync_issue(
                f"Ambiguous manual close fill on {sym} ({event_pos_side or '?'}) — deferred to live sync"
            ))
            asyncio.create_task(sync_live_positions())
            return

        if matched_key is None and fill_qty > 0:
            best = min(candidates, key=lambda x: abs(float(x[1].get("qty", 0)) - fill_qty))
            matched_key = best[0]
            print(f"[Live] fill detection: qty-fallback matched {matched_key} (qty={best[1].get('qty')} fill_qty={fill_qty})")

        if matched_key is None:
            print(f"[Live] fill detection: could not match {order_type} fill on {sym} — triggering immediate sync")
            asyncio.create_task(c.record_sync_issue(
                f"Unmatched {order_type} fill on {sym} ({event_pos_side or '?'}) — manual review recommended"
            ))
            asyncio.create_task(sync_live_positions())
            return

        pos = c.state["open_positions"].get(matched_key)
        if not pos:
            return

        if is_tp_type:
            pos_qty = float(pos.get("qty") or 0)
            executed = float(order.get("z") or fill_qty or 0)
            tol = c._qty_match_tolerance(sym, pos_qty)
            if executed > 0 and pos_qty - executed > tol:
                pos["qty"] = binance_live.round_qty(sym, pos_qty - executed)
                pos["tp_order_id"] = None
                pos["tp_client_algo_id"] = None
                pos["protection_status"] = "partial_tp_fill"
                await c.save_state()
                print(
                    f"[Live] Partial TP {matched_key}: closed {executed:g} @ {fill_price:.4f}, "
                    f"remaining qty={pos['qty']}"
                )
                asyncio.create_task(sync_live_positions())
                return

        sl_oid = pos.get("sl_order_id")
        tp_oid = pos.get("tp_order_id")
        if is_sl_type:
            reason = "SL"
        elif is_tp_type:
            reason = "TP"
        elif inferred_reason:
            reason = inferred_reason
        else:
            vanished = c._vanished_exchange_protection_reason(pos, await _open_algo_ids())
            reason = vanished or "ManualClose"

        # Cancel the complementary order for THIS position only
        cancel_id = tp_oid if reason == "SL" else (sl_oid if reason == "TP" else sl_oid)
        if cancel_id:
            try:
                await binance_live.cancel_algo_order(c._http_client, algo_id=cancel_id)
            except Exception as e:
                print(f"[Live] cancel complementary order {cancel_id} for {matched_key}: {e}")

        await c._close_position_unsafe(
            matched_key, fill_price, reason,
            skip_exchange=True,
            realized_pnl=realized_pnl,
            fill_commission_usd=fill_commission,
            fill_fee_usd=fill_fee_usd if fill_fee_usd > 0 else None,
            close_order_id=order_id,
        )
        print(f"[Live {reason} HIT] {matched_key} @ {fill_price:.4f}")


async def _fetch_exchange_account_rest():
    c = _core()
    """REST snapshot of wallet + position risk (reconciliation / UDS outage fallback)."""
    account = await binance_live.get_account(c._http_client)
    risk = await binance_live.get_position_risk(c._http_client)
    usdt = next((a for a in account.get("assets", []) if a["asset"] == "USDT"), {})
    open_pos = []
    for p in risk:
        if float(p.get("positionAmt", 0)) == 0:
            continue
        sym = p["symbol"]
        lev = int(p.get("leverage") or c._effective_leverage())
        c._symbol_leverage[sym] = lev
        open_pos.append({
            "symbol":           sym,
            "side":             "Long" if float(p["positionAmt"]) > 0 else "Short",
            "positionAmt":      float(p["positionAmt"]),
            "entryPrice":       float(p["entryPrice"]),
            "breakEvenPrice":   float(p.get("breakEvenPrice", 0)),
            "markPrice":        float(p.get("markPrice", 0)),
            "unrealizedProfit": float(p.get("unRealizedProfit", 0)),
            "liquidationPrice": float(p.get("liquidationPrice", 0)),
            "leverage":         lev,
            "marginType":       p.get("marginType", "cross"),
            "isolatedWallet":   float(p.get("isolatedWallet", 0)),
            "isolatedMargin":   float(p.get("isolatedMargin", 0)),
            "notional":         float(p.get("notional", 0)),
            "maintMargin":      float(p.get("maintMargin", 0)),
        })
    avail = float(usdt.get("availableBalance", 0))
    c.exchange_account = {
        "walletBalance":    float(usdt.get("walletBalance", 0)),
        "availableBalance": avail,
        "unrealizedProfit": float(usdt.get("unrealizedProfit", 0)) or sum(p["unrealizedProfit"] for p in open_pos),
        "marginBalance":    float(usdt.get("marginBalance", 0)),
        "positions":        open_pos,
        "low_margin_alert": avail < c.LOW_MARGIN_THRESHOLD,
    }
    c._last_exchange_account_ok_at = c._utc_now_iso()


def _uds_account_fresh(max_age_sec: float | None = None):
    c = _core()
    import time as _time_mod
    if not c._uds_connected:
        return False
    age_limit = float(max_age_sec if max_age_sec is not None else c.UDS_ACCOUNT_FRESH_SEC)
    if c._last_uds_account_update_mono <= 0:
        return False
    return (_time_mod.monotonic() - c._last_uds_account_update_mono) < age_limit


async def user_data_stream_loop():
    c = _core()
    """Listen to the Binance User Data Stream for real order fill events.
    Only runs when c.LIVE_MODE=True. Reconnects automatically on disconnect.
    The listen key is kept alive every 25 minutes (Binance TTL = 30 min).
    """
    if not c.LIVE_MODE:
        return

    ka_task = None
    while True:
        if c._binance_rate_limited():
            await asyncio.sleep(30)
            continue
        try:
            listen_key = await binance_live.create_listen_key(c._http_client)
            ws_url = binance_live.user_data_stream_ws_url(listen_key)
            print(f"[Live] User Data Stream connecting: {ws_url[:72]}...")

            async def _keepalive():
                while True:
                    await asyncio.sleep(25 * 60)
                    try:
                        await binance_live.keepalive_listen_key(c._http_client, listen_key)
                        print("[Live] Listen key refreshed")
                    except Exception as e:
                        print(f"[Live] keepalive error: {e}")

            ka_task = asyncio.create_task(_keepalive())
            c._uds_connected = True
            c._last_uds_connected_at = c._utc_now_iso()

            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=60) as ws:
                user_data_stream_loop._retries = 0  # reset backoff on successful connect
                print("[Live] User Data Stream connected")
                while True:
                    try:
                        # User data streams only emit account/order events, so long
                        # quiet periods are normal. Ping/pong detects dead sockets;
                        # this idle timeout is a final safety reconnect.
                        msg = await asyncio.wait_for(ws.recv(), timeout=30 * 60)
                    except asyncio.TimeoutError:
                        c._uds_connected = False
                        c._last_uds_error_at = c._utc_now_iso()
                        await c.record_error_event(
                            "User Data Stream idle for 30m; reconnecting",
                            severity="warning",
                            source="uds",
                            notify=False,
                        )
                        print("[Live] User Data Stream idle for 30m, reconnecting...")
                        break
                    data = json.loads(msg)
                    ev = data.get("e")
                    if ev == "ORDER_TRADE_UPDATE":
                        await handle_order_update(data["o"])
                    elif ev == "ACCOUNT_UPDATE":
                        c._apply_uds_account_update(data)
                        async with c._state_lock:
                            c._recalculate_unrealized_pnls()

        except Exception as e:
            c._uds_connected = False
            c._last_uds_error_at = c._utc_now_iso()
            await c.record_error_event(
                f"User Data Stream error: {e}",
                severity="warning",
                source="uds",
                notify=True,
            )
            print(f"[Live] User Data Stream error: {e}, reconnecting in 5s...")
            await asyncio.sleep(min(300, 5 * (2 ** getattr(user_data_stream_loop, "_retries", 0))))
            user_data_stream_loop._retries = getattr(user_data_stream_loop, "_retries", 0) + 1
        else:
            user_data_stream_loop._retries = 0  # reset on clean disconnect
        finally:
            if ka_task:
                ka_task.cancel()
                ka_task = None
            # Reconcile any fills that happened during the outage
            if not c._binance_rate_limited():
                try:
                    await sync_live_positions()
                except Exception as _se:
                    print(f"[Live] Post-reconnect sync error: {_se}")


async def _sweep_algo_orders_for(symbol: str, position_side: str):
    c = _core()
    """Cancel dangling algo orders on (symbol, positionSide), preserving SL/TP
    legs of OTHER c.state positions that share the same hedge key (multi-Tab on
    same symbol/side). Returns count of cancelled orders.

    Safe to call after a position close — won't touch other tabs' protections.
    """
    if not c.LIVE_MODE:
        return 0
    cancelled = 0
    try:
        # Collect algoIds still tracked by remaining c.state positions on this key.
        still_tracked: set = set()
        for p in c.state["open_positions"].values():
            if p.get("symbol") != symbol:
                continue
            if c._position_side_from_state(p) != str(position_side).upper():
                continue
            for k in ("sl_order_id", "tp_order_id"):
                v = p.get(k)
                if v is not None:
                    still_tracked.add(int(v))

        data = await binance_live._sreq(c._http_client, "GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol})
        orders = c._active_algo_orders(data)
        for o in orders:
            if str(o.get("positionSide") or "").upper() != str(position_side).upper():
                continue
            aid = int(o.get("algoId", 0) or 0)
            if not aid:
                continue
            if aid in still_tracked:
                continue  # another tab on same hedge key owns this leg
            try:
                await binance_live.cancel_algo_order(c._http_client, algo_id=aid)
                print(f"[Sweep] Cancelled dangling {o.get('orderType')} algoId={aid} on {symbol} {position_side}")
                cancelled += 1
            except Exception as e:
                print(f"[Sweep] Cancel fail algoId={aid} on {symbol}: {e}")
    except Exception as e:
        print(f"[Sweep] {symbol} {position_side} fetch error: {e}")
    return cancelled


async def purge_orphaned_algo_orders():
    c = _core()
    """Cancel duplicate SL/TP orders that the bot isn't tracking.

    Two-pass:
      Pass 1 — held-key duplicates: for each (symbol, positionSide) that IS in
        c.state, cancel algo orders whose algoId isn't tracked.
      Pass 2 — dangling orphans: for USDT pairs where positionAmt == 0 AND not
        in c.state, cancel any leftover algo orders (safe — nothing to protect).

    This prevents wiping:
      - Manual orders the user placed on symbols with live positions we don't know about
      - Orders on symbols the bot doesn't trade (only USDT pairs swept in pass 2)
    """
    if not c.LIVE_MODE:
        return
    try:
        # Map (symbol, positionSide) → set of tracked algo_ids for that key
        tracked_by_key: dict = {}
        held_keys: set = set()
        for pos in c.state["open_positions"].values():
            key = (pos.get("symbol"), c._position_side_from_state(pos))
            held_keys.add(key)
            bucket = tracked_by_key.setdefault(key, set())
            for k in ("sl_order_id", "tp_order_id"):
                v = pos.get(k)
                if v is not None:
                    bucket.add(int(v))

        if not held_keys:
            print("[Purge] No c.state positions — skipping (nothing to de-duplicate against)")
            return

        data = await binance_live._sreq(c._http_client, "GET", "/fapi/v1/openAlgoOrders", {})
        open_algo_orders = await c._verified_active_algo_orders(c._http_client, data)

        try:
            exchange_pos = await binance_live.get_position_risk(c._http_client)
        except Exception as e:
            print(f"[Purge] positionRisk fetch failed; active-position safety enabled, pass 2 skipped: {e}")
            exchange_pos = None
        live_pos_keys = set()
        live_qty_by_key: dict = {}
        if exchange_pos is not None:
            live_pos_keys = {
                (p["symbol"], str(p.get("positionSide") or "").upper())
                for p in exchange_pos if float(p.get("positionAmt", 0) or 0) != 0
            }
            live_qty_by_key = {
                (p["symbol"], str(p.get("positionSide") or "").upper()): abs(float(p.get("positionAmt", 0) or 0))
                for p in exchange_pos if float(p.get("positionAmt", 0) or 0) != 0
            }

        cancelled = 0
        preserved = 0
        for o in open_algo_orders:
            algo_id = int(o.get("algoId", 0) or 0)
            if not algo_id:
                continue
            sym = o.get("symbol", "")
            pos_side = str(o.get("positionSide") or "").upper()
            key = (sym, pos_side)
            # Only touch orders whose (sym, side) matches a c.state position AND the
            # algoId isn't one we tracked for that position.
            if key not in held_keys:
                continue
            if algo_id in tracked_by_key.get(key, set()):
                continue
            if key in live_pos_keys:
                order_tab = c._strategy_from_algo_order(o)
                live_qty = live_qty_by_key.get(key, 0.0)
                order_qty = c._algo_quantity(o)
                oversized = live_qty > 0 and order_qty > live_qty + max(live_qty * 1e-9, 1e-12)
                bot_owned = order_tab in c.STRATEGY_LABELS
                if c._policy_cancels_untracked_exchange_algos() or oversized or bot_owned:
                    try:
                        await binance_live.cancel_algo_order(c._http_client, algo_id=algo_id)
                        print(
                            f"[Purge] Cancelled untracked {c._algo_order_type(o)} algoId={algo_id} "
                            f"on live {sym} {pos_side}; tab={order_tab or 'unknown'} "
                            f"local_policy={c._policy_cancels_untracked_exchange_algos()} "
                            f"oversized={oversized} bot_owned={bot_owned}"
                        )
                        cancelled += 1
                    except Exception as ce:
                        print(f"[Purge] Failed to cancel untracked algoId={algo_id} on {sym}: {ce}")
                    continue
                preserved += 1
                print(
                    f"[Purge] Preserved untracked {c._algo_order_type(o)} algoId={algo_id} "
                    f"on live {sym} {pos_side}; sync should adopt or operator should review"
                )
                continue
            o_type = o.get("type", "?")
            try:
                await binance_live.cancel_algo_order(c._http_client, algo_id=algo_id)
                print(f"[Purge] Cancelled duplicate {o_type} algoId={algo_id} on {sym} {pos_side}")
                cancelled += 1
            except Exception as ce:
                print(f"[Purge] Failed to cancel algoId={algo_id} on {sym}: {ce}")

        if cancelled:
            print(f"[Purge] Pass 1: removed {cancelled} duplicate algo orders")
        if preserved:
            await c.record_sync_issue(
                f"Preserved {preserved} untracked algo order(s) on live positions; review c.state ownership"
            )

        # Pass 2: dangling orphans — algo orders on (sym, posSide) with
        # no Binance position AND not in c.state. Only USDT-margined to stay
        # out of user's USDC/other manual activity.
        if exchange_pos is None:
            return

        dangling_cancelled = 0
        for o in open_algo_orders:
            sym = o.get("symbol", "")
            if not sym.endswith("USDT"):
                continue  # USDT-only safety
            pos_side = str(o.get("positionSide") or "").upper()
            key = (sym, pos_side)
            if key in held_keys:
                continue  # handled by pass 1
            if key in live_pos_keys:
                continue  # live position not tracked by bot — leave alone (manual/pre-adopt)
            aid = int(o.get("algoId", 0) or 0)
            if not aid:
                continue
            try:
                await binance_live.cancel_algo_order(c._http_client, algo_id=aid)
                print(f"[Purge] Pass 2: cancelled dangling {o.get('orderType')} algoId={aid} on {sym} {pos_side}")
                dangling_cancelled += 1
            except Exception as ce:
                print(f"[Purge] Pass 2: cancel fail algoId={aid} on {sym}: {ce}")

        total = cancelled + dangling_cancelled
        if total == 0:
            print("[Purge] No orphan algo orders found")
        elif dangling_cancelled:
            print(f"[Purge] Pass 2: removed {dangling_cancelled} dangling algo orders (total {total})")
    except Exception as e:
        print(f"[Purge] Error: {e}")


async def sync_live_positions():
    c = _core()
    """On startup reconcile bot c.state with exchange.

    1. Remove positions from c.state that no longer exist on exchange
       (SL/TP hit while bot was offline).
    2. Recover orphaned exchange positions that are not in c.state
       (bot crashed after placing entry but before saving c.state).
    """
    if not c.LIVE_MODE:
        return
    if c._binance_rate_limited():
        print("[Live] Sync skipped — Binance rate limit active")
        return
    print("[Live] Syncing positions with exchange...")
    try:
        from bot.engine.sync_helpers import clear_sync_close_leg_cache
        clear_sync_close_leg_cache()
        exchange_pos = await binance_live.get_position_risk(c._http_client)
        # USDT-margined only: skip USDC-quoted futures (BTCUSDC, ETHUSDC, etc.)
        active_pos   = [p for p in exchange_pos
                        if float(p["positionAmt"]) != 0
                        and str(p.get("symbol", "")).endswith("USDT")]
        live_pos_keys = {c._position_tuple_from_exchange(p) for p in active_pos}
        mark_by_live_key = {
            c._position_tuple_from_exchange(p): float(p.get("markPrice") or 0)
            for p in active_pos
        }

        # --- Store real wallet balance as a reference (do NOT overwrite per-tab balances) ---
        # Per-tab balances track individual strategy PnL; only the total wallet is synced here.
        real_wallet_bal = None
        try:
            account = await binance_live.get_account(c._http_client)
            usdt = next((a for a in account.get("assets", []) if a["asset"] == "USDT"), {})
            real_wallet_bal = float(usdt.get("walletBalance", 0))
        except Exception as e:
            c._note_binance_rate_limit(e)
            print(f"[Live Sync] account fetch failed (continuing stale-position cleanup): {e}")

        local_closed = 0
        stale_removed_keys: list[str] = []
        async with c._state_lock:
            for pos_key, pos in list(c.state["open_positions"].items()):
                if not c._position_needs_local_exit_monitor(pos):
                    continue
                live_key = c._position_tuple_from_state(pos)
                if live_key not in live_pos_keys:
                    continue
                sym = pos.get("symbol") or pos_key.split("_", 1)[0]
                trigger_px = c._price_for_sltp(sym) or await c._fetch_sltp_trigger_price(sym)
                if trigger_px is None and c.SLTP_TRIGGER_PRICE == "mark":
                    trigger_px = mark_by_live_key.get(live_key) or c._position_mark_price(pos)
                reason = c._local_protection_reason(pos, trigger_px)
                if not reason:
                    continue
                print(
                    f"[Startup Local {reason}] {pos_key} price={trigger_px} "
                    f"SL={pos.get('sl')} TP={pos.get('tp')}"
                )
                await c._close_position_unsafe(pos_key, trigger_px, reason)
                local_closed += 1
        if local_closed:
            await c.record_sync_issue(
                f"Closed {local_closed} local-protected position(s) on startup/sync after trigger price crossed SL/TP"
            )
            exchange_pos = await binance_live.get_position_risk(c._http_client)
            active_pos = [p for p in exchange_pos
                          if float(p["positionAmt"]) != 0
                          and str(p.get("symbol", "")).endswith("USDT")]
            live_pos_keys = {c._position_tuple_from_exchange(p) for p in active_pos}
        
        async with c._state_lock:
            # Store the raw wallet balance for dashboard display without destroying tab PnL tracking.
            if real_wallet_bal is not None:
                c.state["balances"]["_wallet"] = real_wallet_bal
            
            # 1. Remove stale c.state positions
            # Basic: position side no longer on exchange at all
            stale = [k for k, v in c.state["open_positions"].items()
                     if c._position_tuple_from_state(v) not in live_pos_keys
                     and not c._is_recent_entry(v)]
            deferred_recent = [
                k for k, v in c.state["open_positions"].items()
                if c._position_tuple_from_state(v) not in live_pos_keys
                and c._is_recent_entry(v)
            ]
            if deferred_recent:
                print(
                    f"[Live Sync] Deferred stale removal for {len(deferred_recent)} recent entr"
                    f"{'y' if len(deferred_recent) == 1 else 'ies'}: {', '.join(deferred_recent[:5])}"
                )

            # Qty-aware: when multiple same-side positions exist, some may have
            # closed while the side is still alive (exchange qty < c.state total qty).
            # Build exchange qty map: (symbol, positionSide) → abs qty
            ex_qty_map: dict = {}
            for ep in active_pos:
                key = c._position_tuple_from_exchange(ep)
                ex_qty_map[key] = abs(float(ep["positionAmt"]))

            # Group c.state positions by (symbol, positionSide)
            from collections import defaultdict
            side_groups: dict = defaultdict(list)
            for pk, pv in c.state["open_positions"].items():
                if pk not in stale:
                    side_groups[c._position_tuple_from_state(pv)].append((pk, pv))

            for side_key, group in side_groups.items():
                if len(group) <= 1:
                    continue   # single position — basic stale check covers it
                ex_qty = ex_qty_map.get(side_key, 0.0)
                state_total_qty = sum(float(pv.get("qty", 0)) for _, pv in group)
                if ex_qty >= state_total_qty - 1e-6:
                    continue   # all positions still open
                recent_members = [(pk, pv) for pk, pv in group if c._is_recent_entry(pv)]
                if recent_members:
                    names = ", ".join(pk for pk, _ in recent_members[:5])
                    print(
                        f"[Live Sync] Deferring qty mismatch for {side_key[0]} {side_key[1]} "
                        f"inside {int(c._SYNC_ENTRY_GRACE_SEC)}s entry grace "
                        f"(ex={ex_qty:.4f} < state_total={state_total_qty:.4f}; recent={names})"
                    )
                    continue
                # Some positions closed: remove oldest-entered first until qtys balance.
                # Oldest-first is the safest heuristic when we can't know exact fill order.
                remaining = ex_qty
                sorted_group = sorted(group, key=lambda x: x[1].get("entry_time", ""))
                for pk, pv in sorted_group:
                    pos_qty = float(pv.get("qty", 0))
                    if remaining < pos_qty - 1e-6:
                        stale.append(pk)
                        print(f"[Live Sync] Qty mismatch: {pk} presumed closed "
                              f"(ex={ex_qty:.4f} < state_total={state_total_qty:.4f})")
                    else:
                        remaining -= pos_qty

            for k in stale:
                pos = c.state["open_positions"].get(k)
                if pos:
                    await c.record_exchange_sync_close(pos)
                print(f"[Live Sync] Removing stale position {k} (not on exchange)")
                c.state["open_positions"].pop(k, None)
                c._mark_position_registry_closed(k, "stale_sync_removed")
            stale_removed_keys = list(stale)

            # Qty-aware recovery: if Binance live qty is larger than the bot's
            # tracked qty for the same hedge side, recover only the excess leg.
            # This catches cases where a later entry filled but c.state/protection
            # adoption only covered older strategy legs.
            excess_recovered = []
            current_groups: dict = defaultdict(list)
            for pk, pv in c.state["open_positions"].items():
                current_groups[c._position_tuple_from_state(pv)].append((pk, pv))
            active_by_key = {c._position_tuple_from_exchange(ep): ep for ep in active_pos}
            for side_key, ep in active_by_key.items():
                group = current_groups.get(side_key, [])
                if not group:
                    continue
                ex_qty = abs(float(ep.get("positionAmt", 0) or 0))
                state_total_qty = sum(float(pv.get("qty", 0) or 0) for _, pv in group)
                excess_qty = binance_live.round_qty(side_key[0], max(ex_qty - state_total_qty, 0.0))
                min_qty = float(binance_live.symbol_info.get(side_key[0], {}).get("min_qty", 0) or 0)
                tol = max(ex_qty * 1e-6, min_qty, 1e-9)
                if excess_qty <= 0 or (ex_qty - state_total_qty) <= tol:
                    continue
                sym, pos_side = side_key
                side = "Long" if pos_side == "LONG" else "Short"
                entry_px = float(ep.get("entryPrice") or 0)
                source = f"exchange qty excess live={ex_qty} c.state={state_total_qty}"
                recovered_key = await c._recover_untracked_live_qty(
                    sym, pos_side, side, entry_px, excess_qty, source
                )
                if recovered_key:
                    excess_recovered.append(recovered_key)

            # 2. Recover orphaned exchange positions
            state_pos_keys = {c._position_tuple_from_state(v) for v in c.state["open_positions"].values()}
            orphaned   = [p for p in active_pos if c._position_tuple_from_exchange(p) not in state_pos_keys]
            for ep in orphaned:
                sym       = ep["symbol"]
                pos_amt   = float(ep["positionAmt"])
                pos_side  = c._position_tuple_from_exchange(ep)[1]
                side      = "Long" if pos_side == "LONG" else "Short"
                entry_px  = float(ep["entryPrice"])
                qty       = abs(pos_amt)
                # Fetch open orders to recover SL/TP order IDs
                sl_oid = tp_oid = None
                sl_px  = tp_px  = 0.0
                sl_client_id = None
                tp_client_id = None
                recovered_tab = None
                recovery_source = None
                try:
                    _sg_data = await binance_live._sreq(
                        c._http_client, "GET", "/fapi/v1/openAlgoOrders", {"symbol": sym}
                    )
                    orders = c._active_algo_orders(_sg_data)
                    for o in orders:
                        order_pos_side = str(o.get("positionSide") or "").upper()
                        if order_pos_side and order_pos_side != pos_side:
                            continue
                        order_tab = c._strategy_from_algo_order(o)
                        if order_tab and not recovered_tab:
                            recovered_tab = order_tab
                            recovery_source = "protective order id"
                        order_type = c._algo_order_type(o)
                        if order_type == "STOP_MARKET":
                            sl_oid = int(o.get("algoId") or o.get("orderId"))
                            sl_px  = c._algo_trigger_price(o)
                            sl_client_id = o.get("clientAlgoId")
                        elif order_type in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT"):
                            tp_oid = int(o.get("algoId") or o.get("orderId"))
                            tp_px  = c._algo_trigger_price(o)
                            tp_client_id = o.get("clientAlgoId")
                except Exception as oe:
                    print(f"[Live Sync] Could not fetch orders for {sym}: {oe}")

                if not recovered_tab:
                    recovered_tab, recovery_source = c._strategy_from_position_registry(
                        sym, pos_side, side, entry_px, qty
                    )

                if not recovered_tab:
                    recovered_tab, recovery_source = await c._strategy_from_recent_entry_order(
                        sym, pos_side, side, entry_px, qty
                    )
                
                if "SafeGuard" not in c.state["balances"]:
                    c.state["balances"]["SafeGuard"] = 0.0
                    c.state["unrealized_pnls"]["SafeGuard"] = 0.0

                missing_sl = sl_oid is None or sl_px == 0.0
                missing_tp = tp_oid is None or tp_px == 0.0
                protection_mode = None
                protection_reason = None
                protection_status = "exchange"

                # ── Auto-Safeguard: put back only the protective legs that are missing ──
                if missing_sl or missing_tp:
                    try:
                        # Try to compute ATR from live klines instead of a local cache file
                        try:
                            klines = await c.get_klines(sym, "1h", limit=50)
                            if klines:
                                df_sg = pd.DataFrame(klines, columns=["timestamp", "open", "high", "low", "close", "volume"])
                                atr_series = strategies._calc_atr(df_sg, 14).dropna()
                                atr = float(atr_series.iloc[-1]) if len(atr_series) > 0 else 0.0
                                if atr > 0 and not (atr != atr):  # guard NaN
                                    if side == "Long":
                                        sl_px = entry_px - (2.1 * atr)
                                        tp_px = entry_px + (3.5 * atr)
                                    else:
                                        sl_px = entry_px + (2.1 * atr)
                                        tp_px = entry_px - (3.5 * atr)
                        except Exception as _atr_err:
                            print(f"[Live Sync] ATR fetch failed for {sym}: {_atr_err}")
                        if sl_px <= 0 or tp_px <= 0:
                            # Fallback if no local cache found: SL 2.5%, TP 5%
                            if side == "Long":
                                sl_px = entry_px * 0.975
                                tp_px = entry_px * 1.050
                            else:
                                sl_px = entry_px * 1.025
                                tp_px = entry_px * 0.950
                                
                        print(
                            f"[Live Sync] Protective orders incomplete for {sym} {pos_side}, "
                            f"applying Auto-Safeguard: SL={sl_px:.4f}, TP={tp_px:.4f}"
                        )

                        rec_mode = c._effective_sltp_mode()
                        rec_algo = await c._open_algo_order_count() if rec_mode != "local" else None
                        rec_plan = c._resolve_entry_protection_plan(rec_mode, rec_algo)
                        if rec_plan is None:
                            rec_plan = True, True, c._FALLBACK_LOCAL_REASON
                        sl_local, tp_local, rec_reason = rec_plan
                        if sl_local and tp_local:
                            protection_mode = "local"
                            protection_reason = rec_reason
                            protection_status = "local"
                            sl_oid = None
                            tp_oid = None
                            sl_client_id = None
                            tp_client_id = None
                            await c.record_sync_issue(
                                f"{sym} {pos_side} orphan recovered with bot-managed local SL/TP ({rec_mode})"
                            )
                        else:
                            close_side = "SELL" if side == "Long" else "BUY"
                            if not sl_local and missing_sl:
                                sl_client_id = (
                                    c._strategy_client_id(recovered_tab, pos_side, "SL")
                                    if recovered_tab else None
                                )
                                sl_kwargs = {"client_algo_id": sl_client_id} if sl_client_id else {}
                                sl_res = await binance_live.place_stop_loss(
                                    c._http_client, sym, close_side, sl_px, qty, position_side=pos_side,
                                    **sl_kwargs,
                                )
                                sl_oid = c._algo_id(sl_res)
                            if not tp_local and missing_tp:
                                tp_client_id = (
                                    c._strategy_client_id(recovered_tab, pos_side, "TP")
                                    if recovered_tab else None
                                )
                                tp_kwargs = {"client_algo_id": tp_client_id} if tp_client_id else {}
                                tp_res = await binance_live.place_exchange_take_profit(
                                    c._http_client, sym, close_side, tp_px, qty, position_side=pos_side,
                                    tp_style=c.SLTP_TP_STYLE, **tp_kwargs,
                                )
                                tp_oid = c._algo_id(tp_res)
                            if sl_local != tp_local:
                                protection_mode = "hybrid"
                                protection_reason = rec_reason
                                protection_status = "hybrid"
                        
                    except Exception as e:
                        print(f"[Live Sync] Failed to auto-calculate SL/TP for {sym}: {e}")

                recovered_tab = recovered_tab or "SafeGuard"
                recovery_source = recovery_source or "SafeGuard fallback"
                pos_key = f"{sym}_{recovered_tab}" if recovered_tab in c.TABS else f"{sym}_{pos_side}_Recovered"
                suffix = 2
                while pos_key in c.state["open_positions"]:
                    pos_key = (
                        f"{sym}_{recovered_tab}_{suffix}"
                        if recovered_tab in c.TABS else f"{sym}_{pos_side}_Recovered_{suffix}"
                    )
                    suffix += 1
                c.state["open_positions"][pos_key] = {
                    "tab": recovered_tab, "symbol": sym, "side": side,
                    "position_side": pos_side,
                    "entry_price": entry_px, "sl": sl_px, "tp": tp_px,
                    "qty": qty, "entry_time": datetime.now().isoformat(),
                    "sl_order_id": sl_oid, "tp_order_id": tp_oid,
                    "sl_client_algo_id": sl_client_id,
                    "tp_client_algo_id": tp_client_id,
                    "recovery_source": recovery_source,
                    "protection_status": protection_status,
                }
                rec_pos = c.state["open_positions"][pos_key]
                if protection_mode:
                    rec_pos["protection_mode"] = protection_mode
                    rec_pos["protection_reason"] = protection_reason
                    if protection_mode == "local":
                        c._apply_protection_sources(
                            rec_pos, sl_local=True, tp_local=True,
                            reason=protection_reason or c._MAINNET_LOCAL_SLTP_REASON,
                        )
                    elif protection_mode == "hybrid":
                        c._apply_protection_sources(
                            rec_pos, sl_local=False, tp_local=True,
                            reason=protection_reason or c._HYBRID_SLTP_REASON,
                        )
                c._upsert_position_registry(
                    pos_key,
                    rec_pos,
                    status="local" if protection_mode == "local" else ("hybrid" if protection_mode == "hybrid" else "open"),
                )
                await c._alert_position_protection_risk(
                    pos_key,
                    c.state["open_positions"][pos_key],
                    source="sync_recovery",
                    notify=True,
                    sync_issue=True,
                )
                print(f"[Live Sync] Secured orphaned position: {sym} {side}/{pos_side} "
                      f"@ {entry_px} qty={qty} SL={sl_px} TP={tp_px}")
                asyncio.create_task(c.record_sync_issue(
                    f"Recovered orphaned {sym} {side}/{pos_side} qty={qty} — labelled {recovered_tab} via {recovery_source}"
                ))

            if stale or orphaned or excess_recovered:
                await c.save_state()

        print(f"[Live] Sync done. Live={live_pos_keys or 'none'} | "
              f"Removed={len(stale)} | Recovered={len(orphaned)} | Excess={len(excess_recovered)}")

        # 3. Repair: for every tracked position, verify SL and TP still exist on
        # the exchange. If either leg is missing (cancelled externally, purged,
        # or filled-then-not-replaced), re-place it using the stored prices.
        # Fetch all open ALGO orders once (SL/TP are algo orders, NOT regular
        # openOrders — /fapi/v1/openAlgoOrders is the correct endpoint).
        try:
            _algo_data = await binance_live._sreq(
                c._http_client, "GET", "/fapi/v1/openAlgoOrders", {}
            )
            _all_algo = c._active_algo_orders(_algo_data)
        except Exception as oe:
            print(f"[Live Sync] Repair: can't fetch algo orders: {oe}")
            _all_algo = None

        adopted = 0
        adjusted_qty = 0
        local_policy_cancelled = 0
        if _all_algo is not None:
            from collections import defaultdict
            live_pos_info = {c._position_tuple_from_exchange(p): p for p in active_pos}
            orders_by_id = {
                int(o.get("algoId", 0) or 0): o
                for o in _all_algo
                if int(o.get("algoId", 0) or 0)
            }
            tracked_ids = set()
            for pos in c.state["open_positions"].values():
                for oid_key in ("sl_order_id", "tp_order_id"):
                    oid = pos.get(oid_key)
                    if oid is not None:
                        tracked_ids.add(int(oid))

            async with c._state_lock:
                # If a recovered aggregate position was tied to only one real
                # protective pair, shrink its qty to that pair so extra pairs can
                # be adopted as separate SafeGuard legs.
                for pos_key, pos in list(c.state["open_positions"].items()):
                    sym = pos.get("symbol")
                    pos_side = c._position_side_from_state(pos)
                    if (sym, pos_side) not in live_pos_keys:
                        continue
                    leg_qtys = []
                    for oid_key in ("sl_order_id", "tp_order_id"):
                        oid = pos.get(oid_key)
                        order = orders_by_id.get(int(oid)) if oid is not None else None
                        if order is not None:
                            q = c._algo_quantity(order)
                            if q > 0:
                                leg_qtys.append(q)
                    if not leg_qtys:
                        continue
                    target_qty = min(leg_qtys)
                    if max(leg_qtys) - target_qty > 1e-9:
                        continue
                    current_qty = float(pos.get("qty", 0) or 0)
                    if target_qty > 0 and abs(current_qty - target_qty) > 1e-9:
                        pos["qty"] = target_qty
                        c._upsert_position_registry(pos_key, pos, status="open")
                        adjusted_qty += 1
                        print(
                            f"[Live Sync] Adjusted {pos_key} qty {current_qty} -> {target_qty} "
                            "to match tracked protective pair"
                        )

                untracked_by_key: dict = defaultdict(list)
                for o in _all_algo:
                    aid = int(o.get("algoId", 0) or 0)
                    if not aid or aid in tracked_ids:
                        continue
                    sym = o.get("symbol")
                    pos_side = str(o.get("positionSide") or "").upper()
                    key = (sym, pos_side)
                    if key not in live_pos_keys:
                        continue
                    if c._algo_order_type(o) not in {"STOP_MARKET", "TAKE_PROFIT_MARKET", "TAKE_PROFIT"}:
                        continue
                    untracked_by_key[key].append(o)

                tp_algo_types = {"TAKE_PROFIT_MARKET", "TAKE_PROFIT"}
                for key, orders in untracked_by_key.items():
                    sym, pos_side = key
                    side = "Long" if pos_side == "LONG" else "Short"
                    live_ep = live_pos_info.get(key, {})
                    entry_px = float(live_ep.get("entryPrice", 0) or 0)
                    sl_orders = sorted(
                        [o for o in orders if c._algo_order_type(o) == "STOP_MARKET"],
                        key=lambda o: int(o.get("createTime", 0) or 0),
                    )
                    tp_orders = sorted(
                        [o for o in orders if c._algo_order_type(o) in tp_algo_types],
                        key=lambda o: int(o.get("createTime", 0) or 0),
                    )
                    used_tp_ids = set()
                    for sl_order in sl_orders:
                        sl_qty = c._algo_quantity(sl_order)
                        if sl_qty <= 0:
                            continue
                        candidates = [
                            tp for tp in tp_orders
                            if int(tp.get("algoId", 0) or 0) not in used_tp_ids
                            and abs(c._algo_quantity(tp) - sl_qty) <= max(sl_qty * 1e-9, 1e-12)
                        ]
                        if not candidates:
                            continue
                        tp_order = min(
                            candidates,
                            key=lambda tp: abs(
                                int(tp.get("createTime", 0) or 0)
                                - int(sl_order.get("createTime", 0) or 0)
                            ),
                        )
                        used_tp_ids.add(int(tp_order.get("algoId")))

                        adopted_tab = (
                            c._strategy_from_algo_order(sl_order)
                            or c._strategy_from_algo_order(tp_order)
                            or "SafeGuard"
                        )
                        if c._policy_cancels_untracked_exchange_algos():
                            for oid in (int(sl_order["algoId"]), int(tp_order["algoId"])):
                                try:
                                    await binance_live.cancel_algo_order(c._http_client, algo_id=oid)
                                    local_policy_cancelled += 1
                                except Exception as cancel_err:
                                    print(
                                        f"[Live Sync] cancel untracked local-policy protection {oid} "
                                        f"for {sym} {pos_side}: {cancel_err}"
                                    )
                            tracked_ids.add(int(sl_order["algoId"]))
                            tracked_ids.add(int(tp_order["algoId"]))
                            continue
                        live_qty = abs(float(live_ep.get("positionAmt", 0) or 0))
                        current_state_qty = sum(
                            float(existing.get("qty", 0) or 0)
                            for existing in c.state["open_positions"].values()
                            if existing.get("symbol") == sym
                            and c._position_side_from_state(existing) == pos_side
                        )
                        if live_qty > 0 and current_state_qty + sl_qty > live_qty + max(live_qty * 1e-9, 1e-12):
                            print(
                                f"[Live Sync] Skip adopting untracked protective pair for {sym} {pos_side}: "
                                f"state_qty={current_state_qty} + pair_qty={sl_qty} > live_qty={live_qty}"
                            )
                            continue
                        pos_key = f"{sym}_{adopted_tab}" if adopted_tab in c.TABS else f"{sym}_{pos_side}_Recovered"
                        suffix = 2
                        while pos_key in c.state["open_positions"]:
                            pos_key = (
                                f"{sym}_{adopted_tab}_{suffix}"
                                if adopted_tab in c.TABS else f"{sym}_{pos_side}_Recovered_{suffix}"
                            )
                            suffix += 1
                        c.state["open_positions"][pos_key] = {
                            "tab": adopted_tab,
                            "symbol": sym,
                            "side": side,
                            "position_side": pos_side,
                            "entry_price": entry_px,
                            "sl": c._algo_trigger_price(sl_order),
                            "tp": c._algo_trigger_price(tp_order),
                            "qty": sl_qty,
                            "entry_time": datetime.now(timezone.utc).isoformat(),
                            "sl_order_id": int(sl_order["algoId"]),
                            "tp_order_id": int(tp_order["algoId"]),
                            "sl_client_algo_id": sl_order.get("clientAlgoId"),
                            "tp_client_algo_id": tp_order.get("clientAlgoId"),
                        }
                        c._upsert_position_registry(pos_key, c.state["open_positions"][pos_key], status="open")
                        tracked_ids.add(int(sl_order["algoId"]))
                        tracked_ids.add(int(tp_order["algoId"]))
                        adopted += 1
                        print(
                            f"[Live Sync] Adopted untracked protective pair as {pos_key}: "
                            f"qty={sl_qty} SL={c._algo_trigger_price(sl_order)} TP={c._algo_trigger_price(tp_order)}"
                        )

            if adopted or adjusted_qty or local_policy_cancelled:
                await c.save_state()
                await c.record_sync_issue(
                    f"Adopted {adopted} untracked protective pair(s); "
                    f"adjusted {adjusted_qty} aggregate qty field(s); "
                    f"cancelled {local_policy_cancelled} local-policy protective leg(s)"
                )

        repaired = 0
        if _all_algo is not None:
            algo_count = len(_all_algo)
            c._dashboard_algo_order_count = algo_count
            for pos_key, pos in list(c.state["open_positions"].items()):
                sym      = pos.get("symbol")
                pos_side = c._position_side_from_state(pos)
                if not sym or not str(sym).endswith("USDT"):
                    continue
                if (sym, pos_side) not in live_pos_keys:
                    continue
                # Per-position check by algoId (not per key) — multiple Tabs
                # can share (sym, posSide) in hedge mode, each owning its own
                # SL/TP leg. Only re-place if THIS position's tracked id isn't
                # on the exchange.
                sl_tracked_id = pos.get("sl_order_id")
                tp_tracked_id = pos.get("tp_order_id")
                alive_ids = set()
                for o in _all_algo:
                    if o.get("symbol") != sym:
                        continue
                    if str(o.get("positionSide") or "").upper() != pos_side:
                        continue
                    aid = int(o.get("algoId", 0) or 0)
                    if aid:
                        alive_ids.add(aid)
                has_sl = sl_tracked_id is not None and int(sl_tracked_id) in alive_ids
                has_tp = tp_tracked_id is not None and int(tp_tracked_id) in alive_ids
                missing_sl = not has_sl
                missing_tp = not has_tp
                missing_legs = int(missing_sl) + int(missing_tp)
                policy_mode = c._effective_sltp_mode()
                target_plan = c._resolve_entry_protection_plan(policy_mode, algo_count)
                if policy_mode == "binance_fallback" and pos.get("sl_source"):
                    sl_tgt_local = c._position_sl_is_local(pos)
                    tp_tgt_local = c._position_tp_is_local(pos)
                    tgt_reason = str(pos.get("protection_reason") or c._FALLBACK_LOCAL_REASON)
                elif target_plan:
                    sl_tgt_local, tp_tgt_local, tgt_reason = target_plan
                else:
                    sl_tgt_local, tp_tgt_local, tgt_reason = False, False, c._BINANCE_EXCHANGE_REASON

                if policy_mode == "local" and (has_sl or has_tp or not c._position_full_local(pos)):
                    for oid in (sl_tracked_id if has_sl else None, tp_tracked_id if has_tp else None):
                        if oid:
                            try:
                                await binance_live.cancel_algo_order(c._http_client, algo_id=oid)
                            except Exception as cancel_err:
                                print(f"[Live Sync] cancel server-side protection {oid} for {pos_key}: {cancel_err}")
                    c._apply_protection_sources(pos, sl_local=True, tp_local=True, reason=c._MAINNET_LOCAL_SLTP_REASON)
                    c._upsert_position_registry(pos_key, pos, status="local")
                    await c.save_state()
                    await c.record_sync_issue(f"{pos_key} switched to local SL/TP (policy)")
                    print(f"[Live Sync] {pos_key} switched to local SL/TP (policy)")
                    continue

                if policy_mode == "hybrid" and (has_tp or not c._position_tp_is_local(pos) or c._position_sl_is_local(pos)):
                    if has_tp and tp_tracked_id:
                        try:
                            await binance_live.cancel_algo_order(c._http_client, algo_id=int(tp_tracked_id))
                        except Exception as cancel_err:
                            print(f"[Live Sync] cancel hybrid TP algo {tp_tracked_id} for {pos_key}: {cancel_err}")
                    c._apply_protection_sources(pos, sl_local=False, tp_local=True, reason=c._HYBRID_SLTP_REASON)
                    has_tp = True
                    missing_tp = False
                    tp_tracked_id = None
                    c._upsert_position_registry(pos_key, pos, status="hybrid")
                    await c.save_state()

                stay_local = sl_tgt_local and tp_tgt_local
                if c._position_full_local(pos) and stay_local:
                    if has_sl or has_tp:
                        for oid in (sl_tracked_id if has_sl else None, tp_tracked_id if has_tp else None):
                            if oid:
                                try:
                                    await binance_live.cancel_algo_order(c._http_client, algo_id=oid)
                                except Exception as cancel_err:
                                    print(f"[Live Sync] cancel server-side protection {oid} for {pos_key}: {cancel_err}")
                        c._apply_protection_sources(pos, sl_local=True, tp_local=True, reason=tgt_reason)
                        c._upsert_position_registry(pos_key, pos, status="local")
                        await c.save_state()
                    continue

                if sl_tgt_local:
                    missing_sl = False
                elif c._position_sl_is_local(pos):
                    missing_sl = True
                else:
                    missing_sl = not has_sl
                if tp_tgt_local:
                    missing_tp = False
                elif c._position_tp_is_local(pos):
                    missing_tp = True
                else:
                    missing_tp = not has_tp
                if not sl_tgt_local or not tp_tgt_local:
                    c._apply_protection_sources(
                        pos, sl_local=sl_tgt_local, tp_local=tp_tgt_local, reason=tgt_reason,
                    )
                missing_legs = int(missing_sl) + int(missing_tp)
                if missing_legs == 0:
                    continue
                close_side = "SELL" if pos.get("side") == "Long" else "BUY"
                qty        = float(pos.get("qty", 0))
                sl_px      = float(pos.get("sl", 0) or 0)
                tp_px      = float(pos.get("tp", 0) or 0)
                live_key = (sym, pos_side)
                trigger_px = c._price_for_sltp(sym) or await c._fetch_sltp_trigger_price(sym)
                if trigger_px is None and c.SLTP_TRIGGER_PRICE == "mark":
                    trigger_px = mark_by_live_key.get(live_key)
                    if trigger_px is None:
                        try:
                            mk = await c._price_feed_get("/fapi/v1/premiumIndex", {"symbol": sym})
                            trigger_px = float(mk.get("markPrice") or 0) or None
                        except Exception:
                            trigger_px = None
                is_long = pos.get("side") == "Long"
                adopted_leg = False
                if missing_sl and sl_px > 0 and qty > 0:
                    existing_sl = await c._find_existing_protective_order(
                        sym, pos_side, "STOP_MARKET", qty, sl_px, _all_algo
                    )
                    if existing_sl:
                        pos["sl_order_id"] = int(existing_sl["algoId"])
                        pos["sl_client_algo_id"] = existing_sl.get("clientAlgoId")
                        has_sl = True
                        missing_sl = False
                        adopted_leg = True
                        print(f"[Live Sync] Adopted existing SL for {pos_key}: algoId={pos['sl_order_id']} @ {sl_px}")
                tp_order_type = "TAKE_PROFIT" if c.SLTP_TP_STYLE == "limit" else "TAKE_PROFIT_MARKET"
                if missing_tp and tp_px > 0 and qty > 0:
                    existing_tp = await c._find_existing_protective_order(
                        sym, pos_side, tp_order_type, qty, tp_px, _all_algo
                    )
                    if existing_tp:
                        pos["tp_order_id"] = int(existing_tp["algoId"])
                        pos["tp_client_algo_id"] = existing_tp.get("clientAlgoId")
                        has_tp = True
                        missing_tp = False
                        adopted_leg = True
                        print(f"[Live Sync] Adopted existing TP for {pos_key}: algoId={pos['tp_order_id']} @ {tp_px}")
                if adopted_leg:
                    if pos.get("sl_order_id") and pos.get("tp_order_id"):
                        pos.pop("protection_mode", None)
                        pos.pop("protection_reason", None)
                        pos["protection_status"] = "exchange"
                    c._upsert_position_registry(pos_key, pos, status=pos.get("protection_status") or "open")
                    await c.save_state()
                missing_legs = int(missing_sl) + int(missing_tp)
                if not missing_legs:
                    continue
                if not has_sl and sl_px > 0 and qty > 0:
                    if c._exchange_sl_crossed_mark(is_long, sl_px, trigger_px):
                        print(
                            f"[Live Sync] Closing {pos_key}: missing SL and price={trigger_px} "
                            f"crossed stored SL={sl_px}"
                        )
                        await c._close_position_unsafe(pos_key, trigger_px or sl_px, "SL")
                        continue
                    if algo_count + repaired >= c._max_open_algo_orders():
                        await c._has_algo_capacity(1, f"{pos_key} SL repair")
                        continue
                    try:
                        sl_client_id = c._strategy_client_id(pos.get("tab"), pos_side, "SL")
                        sl_res = await binance_live.place_stop_loss(
                            c._http_client, sym, close_side, sl_px, qty, position_side=pos_side,
                            client_algo_id=sl_client_id,
                        )
                        pos["sl_order_id"] = c._algo_id(sl_res)
                        pos["sl_client_algo_id"] = sl_client_id
                        if pos.get("sl_order_id") and pos.get("tp_order_id"):
                            pos.pop("protection_mode", None)
                            pos.pop("protection_reason", None)
                            pos["protection_status"] = "exchange"
                            print(f"[Live Sync] {pos_key} upgraded from local fallback to Binance server-side SL/TP")
                        c._upsert_position_registry(pos_key, pos, status="open")
                        repaired += 1
                        print(f"[Live Sync] Repaired missing SL for {pos_key} @ {sl_px}")
                    except Exception as pe:
                        body = c._http_error_text(pe)
                        if c._is_immediate_trigger_error(pe, body):
                            close_px = trigger_px or sl_px
                            print(
                                f"[Live Sync] Closing {pos_key}: SL re-place rejected (-2021) "
                                f"price={close_px} sl={sl_px}"
                            )
                            await c._close_position_unsafe(pos_key, close_px, "SL")
                            continue
                        print(f"[Live Sync] Failed to re-place SL for {pos_key}: {pe} | Binance: {body}")
                        pos["protection_status"] = "verify_warning"
                        c._upsert_position_registry(pos_key, pos, status="verify_warning")
                        await c._alert_position_protection_risk(
                            pos_key,
                            pos,
                            source="protective_repair",
                            notify=True,
                            sync_issue=True,
                        )
                        await c.save_state()
                if not has_tp and tp_px > 0 and qty > 0:
                    if c._exchange_tp_crossed_mark(is_long, tp_px, trigger_px):
                        print(
                            f"[Live Sync] Closing {pos_key}: missing TP and price={trigger_px} "
                            f"crossed stored TP={tp_px}"
                        )
                        await c._close_position_unsafe(pos_key, trigger_px or tp_px, "TP")
                        continue
                    if algo_count + repaired >= c._max_open_algo_orders():
                        await c._has_algo_capacity(1, f"{pos_key} TP repair")
                        continue
                    try:
                        tp_client_id = c._strategy_client_id(pos.get("tab"), pos_side, "TP")
                        tp_res = await binance_live.place_exchange_take_profit(
                            c._http_client, sym, close_side, tp_px, qty, position_side=pos_side,
                            tp_style=c.SLTP_TP_STYLE, client_algo_id=tp_client_id,
                        )
                        pos["tp_order_id"] = c._algo_id(tp_res)
                        pos["tp_client_algo_id"] = tp_client_id
                        if pos.get("sl_order_id") and pos.get("tp_order_id"):
                            pos.pop("protection_mode", None)
                            pos.pop("protection_reason", None)
                            pos["protection_status"] = "exchange"
                            print(f"[Live Sync] {pos_key} upgraded from local fallback to Binance server-side SL/TP")
                        c._upsert_position_registry(pos_key, pos, status="open")
                        repaired += 1
                        print(f"[Live Sync] Repaired missing TP for {pos_key} @ {tp_px}")
                    except Exception as pe:
                        body = c._http_error_text(pe)
                        if c._is_immediate_trigger_error(pe, body):
                            close_px = trigger_px or tp_px
                            print(
                                f"[Live Sync] Closing {pos_key}: TP re-place rejected (-2021) "
                                f"price={close_px} tp={tp_px}"
                            )
                            await c._close_position_unsafe(pos_key, close_px, "TP")
                            continue
                        print(f"[Live Sync] Failed to re-place TP for {pos_key}: {pe} | Binance: {body}")
                        pos["protection_status"] = "verify_warning"
                        c._upsert_position_registry(pos_key, pos, status="verify_warning")
                        await c._alert_position_protection_risk(
                            pos_key,
                            pos,
                            source="protective_repair",
                            notify=True,
                            sync_issue=True,
                        )
                        await c.save_state()
            if repaired:
                await c.save_state()
                print(f"[Live] Repaired {repaired} missing protective legs")

        # Purge algo orders that have no matching position in c.state
        await c.purge_orphaned_algo_orders()
        try:
            from bot.engine.entry import reconcile_pending_entry_orders
            await reconcile_pending_entry_orders()
        except Exception as pe:
            print(f"[Limit Entry] reconcile during sync: {pe}")
        if c._prune_sync_issues(stale_removed_keys):
            try:
                await c.save_state()
            except Exception:
                pass
        c._last_sync_ok_at = c._utc_now_iso()

    except Exception as e:
        c._last_sync_error_at = c._utc_now_iso()
        await c.record_error_event(
            f"Live position sync error: {e}",
            severity="critical",
            source="sync_live_positions",
            notify=True,
        )
        print(f"[Live] Sync error: {e}")
