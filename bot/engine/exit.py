"""Position close, PnL reconcile, and rate-limit helpers (extracted from bot.core)."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

import binance_live
from config import (
    BINANCE_IP_BAN_DEFAULT_BACKOFF_SEC,
    CIRCUIT_BREAKER_DAILY_LOSS,
    CLOSE_ALL_PREFLIGHT,
    CLOSE_ALL_RETRY_SEC,
    CLOSE_ALL_STAGGER_SEC,
    EXIT_FEE_MAKER_PCT,
    EXIT_FEE_TAKER_PCT,
    PNL_REPAIR_BATCH_PAUSE_SEC,
    PNL_REPAIR_BATCH_SIZE,
    PNL_REPAIR_DEFER_POLL_SEC,
    PNL_REPAIR_ENTRY_DELAY_SEC,
    PNL_REPAIR_STARTUP_DELAY_SEC,
    SLIPPAGE_PCT,
)

def _core():
    from bot import core
    return core


def _close_side_for_position(side: str):
    c = _core()
    return "SELL" if str(side).lower() == "long" else "BUY"


def _consumed_close_trade_ids(
    sym: str,
    *,
    skip_tab: str | None = None,
    skip_exit_time: str | None = None,
):
    c = _core()
    """Trade ids already attributed to another bot close on this symbol."""
    consumed: set[int] = set()
    for entry in c.state.get("history", []):
        if entry.get("symbol") != sym:
            continue
        if skip_tab and entry.get("tab") == skip_tab and entry.get("exit_time") == skip_exit_time:
            continue
        for tid in entry.get("close_trade_ids") or []:
            try:
                consumed.add(int(tid))
            except (TypeError, ValueError):
                pass
    return consumed


async def _resolve_close_order_id(
    sym: str,
    order_id: int | None,
    client_order_id: str | None,
):
    c = _core()
    """Resolve Binance orderId for a bot close (from stored id or origClientOrderId)."""
    if order_id not in (None, "", 0):
        try:
            return int(order_id)
        except (TypeError, ValueError):
            pass
    if not client_order_id or not c._http_client:
        return None
    try:
        order = await binance_live.get_order(
            c._http_client,
            sym,
            orig_client_order_id=str(client_order_id),
        )
        oid = int(order.get("orderId") or 0)
        return oid or None
    except Exception as e:
        print(f"[PnL Reconcile] order lookup failed {sym} client={client_order_id}: {e}")
        return None


def _match_close_trades(
    trades: list,
    *,
    pos_side: str,
    close_side: str,
    qty: float,
    exit_ms: int,
    order_id: int | None = None,
    exclude_trade_ids: set[int] | None = None,
    strict_order: bool = False,
):
    c = _core()
    """Pick userTrades rows that belong to one bot close (hedge-safe)."""
    excluded = exclude_trade_ids or set()
    candidates = []
    for trade in trades:
        trade_id = int(trade.get("id") or 0)
        if trade_id in excluded:
            continue
        if str(trade.get("positionSide") or "").upper() != str(pos_side).upper():
            continue
        if str(trade.get("side") or "").upper() != str(close_side).upper():
            continue
        candidates.append(trade)
    if not candidates:
        return []

    if order_id is not None:
        by_order = [
            t for t in candidates
            if int(t.get("orderId") or 0) == int(order_id)
        ]
        if by_order:
            return by_order
        if strict_order:
            return []

    if strict_order:
        return []

    qty = float(qty or 0)
    qty_tol = max(qty * 0.002, 1e-9) if qty > 0 else 1e-9

    by_oid: dict[int, list] = {}
    for trade in candidates:
        oid = int(trade.get("orderId") or 0)
        by_oid.setdefault(oid, []).append(trade)

    best_group = None
    best_score = None
    for group in by_oid.values():
        group_qty = sum(float(t.get("qty") or 0) for t in group)
        if qty > 0 and abs(group_qty - qty) > qty_tol:
            continue
        group_ms = min(int(t.get("time") or 0) for t in group)
        score = abs(group_ms - exit_ms)
        if best_score is None or score < best_score:
            best_score = score
            best_group = group
    if best_group:
        return best_group

    if qty <= 0:
        return [min(candidates, key=lambda t: abs(int(t.get("time") or 0) - exit_ms))]

    sorted_trades = sorted(candidates, key=lambda t: abs(int(t.get("time") or 0) - exit_ms))
    picked = []
    acc_qty = 0.0
    for trade in sorted_trades:
        picked.append(trade)
        acc_qty += float(trade.get("qty") or 0)
        if abs(acc_qty - qty) <= qty_tol:
            return picked
        if acc_qty > qty + qty_tol:
            break
    return picked


_STABLE_COMMISSION_ASSETS = frozenset({"USDT", "BUSD", "USDC", "FDUSD"})


def _commission_asset_usd_rate(asset: str):
    c = _core()
    """USDT price of one unit of commission asset (1.0 for stables)."""
    asset = str(asset or "USDT").upper()
    if asset in _STABLE_COMMISSION_ASSETS:
        return 1.0
    if asset == "BNB":
        rate = c.latest_prices.get("BNBUSDT") or c.latest_marks.get("BNBUSDT")
        if rate and float(rate) > 0:
            return float(rate)
    return None


def _trade_commission_parts(trade: dict):
    c = _core()
    """Per userTrade: (USDT-wallet signed commission, fee USD for display)."""
    raw = float(trade.get("commission") or 0)
    if abs(raw) < 1e-15:
        return 0.0, 0.0
    asset = str(trade.get("commissionAsset") or "USDT").upper()
    rate = _commission_asset_usd_rate(asset)
    if rate is None:
        return 0.0, 0.0
    usd_signed = raw * rate
    usdt_signed = usd_signed if asset in _STABLE_COMMISSION_ASSETS else 0.0
    return usdt_signed, abs(usd_signed)


def _sum_trades_commission_parts(trades: list):
    c = _core()
    """Sum userTrades: (USDT-wallet signed commission, total fee USD for display)."""
    usdt_total = 0.0
    fee_total = 0.0
    for trade in trades:
        usdt_part, fee_part = _trade_commission_parts(trade)
        usdt_total += usdt_part
        fee_total += fee_part
    return usdt_total, fee_total


def _fill_net_pnl(realized_pnl: float | None, commission: float | None = None):
    c = _core()
    """Binance per-fill net: realizedPnl/rp plus commission (commission is usually negative)."""
    if realized_pnl is None:
        return None
    return float(realized_pnl) + float(commission or 0)


def _order_commission_parts(order: dict):
    c = _core()
    """ORDER_TRADE_UPDATE commission: (USDT-wallet signed, fee USD for display)."""
    raw = order.get("n")
    if raw in (None, "", 0):
        return None, 0.0
    usdt_part, fee_part = _trade_commission_parts({
        "commission": raw,
        "commissionAsset": order.get("N") or "USDT",
    })
    if usdt_part != 0.0:
        return usdt_part, fee_part
    asset = str(order.get("N") or "USDT").upper()
    if asset in _STABLE_COMMISSION_ASSETS:
        return float(raw), abs(float(raw))
    return None, fee_part


def _order_fill_commission_usd(order: dict):
    c = _core()
    """USDT-wallet commission from ORDER_TRADE_UPDATE (stables only; BNB fees are separate)."""
    usdt_part, _ = _order_commission_parts(order)
    return usdt_part


async def _fetch_commission_income_usd(sym: str, exit_ms: int, *, window_ms: int = 8000):
    c = _core()
    """Signed COMMISSION income near exit (USDT-equivalent) when userTrades commission is empty."""
    if not c._http_client or _binance_rate_limited():
        return 0.0
    try:
        records = await binance_live.get_income(
            c._http_client,
            income_type="COMMISSION",
            symbol=sym,
            start_time=exit_ms - window_ms,
            end_time=exit_ms + window_ms,
            limit=100,
        )
    except Exception as e:
        print(f"[PnL Reconcile] commission income fetch failed {sym}: {e}")
        return 0.0
    total = 0.0
    for record in records or []:
        asset = str(record.get("asset") or "USDT").upper()
        rate = _commission_asset_usd_rate(asset)
        if rate is None:
            continue
        total += float(record.get("income") or 0) * rate
    return total


def _summarize_close_trades(trades: list):
    c = _core()
    if not trades:
        return None
    realized = sum(float(t.get("realizedPnl") or 0) for t in trades)
    usdt_commission, fee_usd = _sum_trades_commission_parts(trades)
    trade_qty = sum(float(t.get("qty") or 0) for t in trades)
    quote_sum = sum(float(t.get("quoteQty") or 0) for t in trades)
    exit_price = (quote_sum / trade_qty) if trade_qty else float(trades[-1].get("price") or 0)
    trade_ids = [int(t.get("id") or 0) for t in trades if int(t.get("id") or 0) > 0]
    return {
        "realized_pnl": realized,
        "commission": usdt_commission,
        "fee_usd": fee_usd,
        "net_pnl": realized + usdt_commission,
        "exit_price": exit_price,
        "trade_qty": trade_qty,
        "trade_count": len(trades),
        "trade_ids": trade_ids,
    }


def _scale_close_summary(summary: dict, target_qty: float):
    c = _core()
    matched_qty = float(summary.get("trade_qty") or 0)
    target_qty = float(target_qty or 0)
    if matched_qty <= 0 or target_qty <= 0:
        return summary
    qty_tol = max(target_qty * 0.002, 1e-9)
    if abs(matched_qty - target_qty) <= qty_tol:
        return summary
    ratio = target_qty / matched_qty
    scaled = dict(summary)
    scaled["realized_pnl"] = float(summary["realized_pnl"]) * ratio
    scaled["commission"] = float(summary["commission"]) * ratio
    scaled["fee_usd"] = float(summary.get("fee_usd") or 0) * ratio
    scaled["net_pnl"] = float(summary["net_pnl"]) * ratio
    scaled["trade_qty"] = target_qty
    return scaled


def _history_qty_for_entry(entry: dict):
    c = _core()
    qty = float(entry.get("qty") or 0)
    if qty > 0:
        return qty
    sym = entry.get("symbol")
    tab = entry.get("tab")
    entry_time = entry.get("entry_time")
    pos_side = entry.get("position_side")
    for reg in c._position_registry().values():
        if reg.get("symbol") != sym or reg.get("tab") != tab:
            continue
        if pos_side and str(reg.get("position_side") or "").upper() != str(pos_side).upper():
            continue
        if entry_time and reg.get("entry_time") != entry_time:
            continue
        reg_qty = float(reg.get("qty") or 0)
        if reg_qty > 0:
            return reg_qty
    entry_px = float(entry.get("entry_price") or 0)
    if entry_px > 0:
        return c._effective_notional_size() / entry_px
    return 0.0


def _find_history_entry(
    sym: str,
    pos_side: str,
    tab: str,
    exit_time_iso: str | None,
):
    c = _core()
    for entry in reversed(c.state.get("history", [])):
        if entry.get("symbol") != sym:
            continue
        if str(entry.get("position_side") or "").upper() != str(pos_side).upper():
            continue
        if tab and entry.get("tab") != tab:
            continue
        if exit_time_iso and entry.get("exit_time") != exit_time_iso:
            continue
        return entry
    return None


async def _fetch_close_pnl_from_trades(
    sym: str,
    pos_side: str,
    side: str,
    qty: float,
    exit_time_iso: str,
    order_id: int | None = None,
    client_order_id: str | None = None,
    exclude_trade_ids: set[int] | None = None,
    *,
    tab: str | None = None,
    ignore_entry_window: bool = False,
):
    c = _core()
    if not c._http_client or _binance_rate_limited():
        return None
    if not ignore_entry_window and c._entry_window_active():
        return None
    exit_ms = c._dt_to_ms(exit_time_iso)
    if exit_ms is None:
        return None
    close_side = c._close_side_for_position(side)
    resolved_oid = await _resolve_close_order_id(sym, order_id, client_order_id)
    strict = resolved_oid is not None or bool(client_order_id)
    excluded = set(exclude_trade_ids or set())
    excluded.update(
        _consumed_close_trade_ids(sym, skip_tab=tab, skip_exit_time=exit_time_iso)
    )
    try:
        trades = await binance_live.get_account_trades(
            c._http_client,
            sym,
            start_time=exit_ms - 120_000,
            end_time=exit_ms + 180_000,
            limit=1000,
        )
    except Exception as e:
        print(f"[PnL Reconcile] trade fetch failed {sym}: {e}")
        return None
    matched = _match_close_trades(
        trades,
        pos_side=pos_side,
        close_side=close_side,
        qty=qty,
        exit_ms=exit_ms,
        order_id=resolved_oid,
        exclude_trade_ids=excluded,
        strict_order=strict,
    )
    summary = _summarize_close_trades(matched)
    if summary is None:
        return None
    if float(summary.get("fee_usd") or 0) < 1e-9:
        income_comm = await _fetch_commission_income_usd(sym, exit_ms)
        if abs(income_comm) > 1e-9:
            summary["fee_usd"] = abs(income_comm)
    if resolved_oid is not None:
        summary["close_order_id"] = resolved_oid
    if client_order_id:
        summary["close_client_order_id"] = client_order_id
    return _scale_close_summary(summary, qty)


async def _apply_history_pnl_update(
    entry: dict,
    summary: dict,
    provisional_pnl: float,
    *,
    label: str,
):
    c = _core()
    net_pnl = float(summary["net_pnl"])
    realized = float(summary["realized_pnl"])
    if abs(realized) < 0.001 and abs(provisional_pnl) > 0.05 and abs(net_pnl) < 0.05:
        print(
            f"[PnL Reconcile] {label}: realized~0 but provisional=${provisional_pnl:.4f}; "
            "keeping provisional"
        )
        return False

    target_qty = float(entry.get("qty") or 0)
    matched_qty = float(summary.get("trade_qty") or 0)
    has_close_key = bool(entry.get("close_order_id") or entry.get("close_client_order_id"))
    if has_close_key and target_qty > 0 and matched_qty > 0:
        qty_tol = max(target_qty * 0.05, 1e-9)
        if matched_qty + qty_tol < target_qty:
            print(
                f"[PnL Reconcile] {label}: matched qty {matched_qty:g} << position {target_qty:g}; "
                f"keeping provisional ${provisional_pnl:.4f}"
            )
            return False
    if abs(provisional_pnl) > 0.50 and abs(net_pnl) < abs(provisional_pnl) * 0.25:
        print(
            f"[PnL Reconcile] {label}: reconcile ${net_pnl:.4f} << provisional ${provisional_pnl:.4f}; "
            "keeping provisional"
        )
        return False

    old_pnl = float(entry.get("pnl_usd") or 0)
    diff = net_pnl - old_pnl
    if abs(diff) < 0.001:
        return False

    tab = entry.get("tab")
    entry["pnl_usd"] = net_pnl
    entry["fee_usd"] = float(
        summary.get("fee_usd") if summary.get("fee_usd") is not None
        else abs(float(summary.get("commission") or 0))
    )
    entry["realized_only"] = realized
    if float(summary.get("exit_price") or 0) > 0:
        entry["exit_price"] = float(summary["exit_price"])
    trade_ids = summary.get("trade_ids") or []
    if trade_ids:
        entry["close_trade_ids"] = [int(t) for t in trade_ids if int(t) > 0]
    if summary.get("close_order_id"):
        entry["close_order_id"] = int(summary["close_order_id"])
    if summary.get("close_client_order_id"):
        entry["close_client_order_id"] = str(summary["close_client_order_id"])
    if tab in c.state.get("balances", {}):
        c.state["balances"][tab] += diff
    if tab:
        if c._use_binance_close_cache():
            c._rebuild_tab_stats_from_binance_closes()
        else:
            c._rebuild_tab_stats_for_tab(str(tab))
    oid_note = ""
    if entry.get("close_order_id"):
        oid_note = f" orderId={entry.get('close_order_id')}"
    elif entry.get("close_client_order_id"):
        oid_note = f" client={entry.get('close_client_order_id')}"
    fee_note = float(entry.get("fee_usd") or 0)
    print(
        f"[PnL Reconcile] {label}: ${old_pnl:.4f} → ${net_pnl:.4f} "
        f"(realized={realized:.4f} comm={float(summary.get('commission') or 0):.4f} "
        f"fee=${fee_note:.4f} trades={summary.get('trade_count', 0)} qty={matched_qty:g}{oid_note})"
    )
    return True


def _history_close_snapshot(
    sym: str,
    pos_side: str,
    tab: str,
    exit_time_iso: str,
    provisional_pnl: float,
    *,
    fallback_exit_price: float = 0.0,
    fallback_fee_usd: float = 0.0,
):
    c = _core()
    entry = _find_history_entry(sym, pos_side, tab, exit_time_iso)
    if entry is None:
        return {
            "net_pnl": float(provisional_pnl),
            "exit_price": float(fallback_exit_price),
            "fee_usd": float(fallback_fee_usd),
        }
    return {
        "net_pnl": float(entry.get("pnl_usd") if entry.get("pnl_usd") is not None else provisional_pnl),
        "exit_price": float(entry.get("exit_price") or fallback_exit_price or 0),
        "fee_usd": float(entry.get("fee_usd") or fallback_fee_usd or 0),
    }


async def _reconcile_pnl_from_binance(
    sym: str,
    pos_side: str,
    tab: str,
    entry_time_iso: str,
    exit_time_iso: str,
    provisional_pnl: float,
    qty: float | None = None,
    order_id: int | None = None,
    *,
    skip_entry_window: bool = False,
    fallback_exit_price: float = 0.0,
    fallback_fee_usd: float = 0.0,
):
    c = _core()
    """Reconcile one close against /fapi/v1/userTrades (per order/qty, hedge-safe)."""
    snapshot = {
        "net_pnl": float(provisional_pnl),
        "exit_price": float(fallback_exit_price),
        "fee_usd": float(fallback_fee_usd),
    }
    if not c.LIVE_MODE:
        return snapshot

    retry_delays = (6, 3, 3) if skip_entry_window else (6,)
    summary = None
    try:
        for delay in retry_delays:
            await asyncio.sleep(delay)
            if not skip_entry_window:
                await c._await_entry_window_clear("PnL Reconcile")

            entry = _find_history_entry(sym, pos_side, tab, exit_time_iso)
            if entry is None:
                print(f"[PnL Reconcile] {sym} {tab}: history entry not found")
                return snapshot

            close_qty = float(qty or entry.get("qty") or 0)
            if close_qty <= 0:
                close_qty = _history_qty_for_entry(entry)
            close_order_id = order_id
            close_client_id = entry.get("close_client_order_id")
            if close_order_id is None:
                stored_oid = entry.get("close_order_id")
                if stored_oid not in (None, "", 0):
                    close_order_id = int(stored_oid)

            summary = await _fetch_close_pnl_from_trades(
                sym,
                pos_side,
                entry.get("side") or ("Long" if pos_side == "LONG" else "Short"),
                close_qty,
                exit_time_iso,
                order_id=close_order_id,
                client_order_id=close_client_id,
                tab=tab,
                ignore_entry_window=skip_entry_window,
            )
            if summary is not None:
                break

        if summary is None:
            if abs(provisional_pnl) > 0.01:
                print(
                    f"[PnL Reconcile] {sym} {tab}: no matching userTrades; "
                    f"keeping ${provisional_pnl:.4f}"
                )
            return _history_close_snapshot(
                sym,
                pos_side,
                tab,
                exit_time_iso,
                provisional_pnl,
                fallback_exit_price=fallback_exit_price,
                fallback_fee_usd=fallback_fee_usd,
            )

        async with c._state_lock:
            target = _find_history_entry(sym, pos_side, tab, exit_time_iso)
            if target is None:
                return snapshot
            changed = await _apply_history_pnl_update(
                target,
                summary,
                provisional_pnl,
                label=f"{sym} {pos_side} {tab}",
            )
        if changed:
            await c.save_state()
    except Exception as e:
        print(f"[PnL Reconcile] {sym} {tab}: {e}")

    return _history_close_snapshot(
        sym,
        pos_side,
        tab,
        exit_time_iso,
        provisional_pnl,
        fallback_exit_price=fallback_exit_price,
        fallback_fee_usd=fallback_fee_usd,
    )


async def _finalize_exit_notify(
    *,
    pos_key: str,
    sym: str,
    tab: str,
    side: str,
    pos_side: str,
    reason: str,
    entry_time_iso: str | None,
    exit_time_iso: str,
    entry_price: float,
    provisional_exit_price: float,
    provisional_net_pnl: float,
    provisional_fee_usd: float,
    close_qty: float,
    close_order_id: int | None,
    placed_sl: float | None = None,
    placed_tp: float | None = None,
):
    c = _core()
    """Reconcile Binance userTrades, then print [EXIT] and send Telegram with final PnL."""
    final = await c._reconcile_pnl_from_binance(
        sym,
        pos_side,
        tab,
        entry_time_iso or "",
        exit_time_iso,
        provisional_net_pnl,
        qty=close_qty,
        order_id=close_order_id,
        skip_entry_window=True,
        fallback_exit_price=provisional_exit_price,
        fallback_fee_usd=provisional_fee_usd,
    )
    net_pnl = float(final["net_pnl"])
    exit_price = float(final["exit_price"] or provisional_exit_price)
    fee_usd = float(final["fee_usd"])

    c._log_exit_close(
        pos_key=pos_key,
        sym=sym,
        tab=tab,
        side=side,
        reason=reason,
        entry_time_iso=entry_time_iso,
        exit_time_iso=exit_time_iso,
        entry_price=entry_price,
        exit_price=exit_price,
        net_pnl=net_pnl,
        fee_usd=fee_usd,
        placed_sl=placed_sl,
        placed_tp=placed_tp,
    )

    if c._is_tp_sl_exit_reason(reason):
        icon = c._telegram_exit_icon(reason, net_pnl)
        sign = "+" if net_pnl >= 0 else ""
        move_pct, _ = c._exit_move_pct_from_entry(side, entry_price, exit_price)
        entry_px = c._log_price(sym, entry_price)
        exit_px = c._log_price(sym, exit_price)
        slip_info = c._exit_target_slip_from_fill(
            reason=reason,
            side=side,
            entry_price=entry_price,
            exit_price=exit_price,
            placed_sl=placed_sl,
            placed_tp=placed_tp,
            sym=sym,
        )
        target_block = ""
        if slip_info:
            target_block = (
                f"🎯 Target {slip_info['label']}: <code>{slip_info['target_px']}</code> "
                f"({slip_info['target_pct']} from fill)\n"
                f"📏 Slip vs target: <b>{slip_info['slip_pct_str']}</b> "
                f"({slip_info['slip_delta_str']})\n"
            )
        await c.send_telegram(
            f"{icon} <b>ปิดออเดอร์สำเร็จ</b>\n"
            f"🧠 Strategy: <b>{tab} — {c._strategy_label(tab)}</b>\n"
            f"📌 Symbol: <b>{sym}</b> | Side: <b>{side}</b>\n"
            f"🏁 Reason: <b>{reason}</b>\n"
            f"📥 Entry: <code>{entry_px}</code>\n"
            f"{target_block}"
            f"💵 Exit: <code>{exit_px}</code>\n"
            f"📈 Move: <b>{move_pct}</b>\n"
            f"📊 PnL: <b>{sign}${net_pnl:.2f}</b>",
            exit_reason=reason,
        )

    if net_pnl < 0:
        c._daily_loss_usd += abs(net_pnl)
        tripped = False
        if not c._circuit_breaker and c._daily_loss_usd >= c.CIRCUIT_BREAKER_DAILY_LOSS:
            c._circuit_breaker = True
            tripped = True
            print(f"[Circuit Breaker] ⛔ Daily loss limit ${c.CIRCUIT_BREAKER_DAILY_LOSS:.0f} reached "
                  f"(total loss today: ${c._daily_loss_usd:.2f}). Trading HALTED.")
        c._persist_circuit_breaker()
        await c.save_state()
        if tripped:
            await c.send_telegram(
                f"⛔ <b>CIRCUIT BREAKER ACTIVATED</b>\n"
                f"Daily loss: <b>${c._daily_loss_usd:.2f}</b> exceeded limit ${c.CIRCUIT_BREAKER_DAILY_LOSS:.0f}\n"
                f"Trading halted until midnight UTC.",
                is_error=True,
            )


def _registry_row_for_history(entry: dict):
    c = _core()
    sym = entry.get("symbol")
    tab = entry.get("tab")
    if not sym or not tab:
        return None
    entry_time = entry.get("entry_time")
    pos_side = str(entry.get("position_side") or "").upper()
    for reg in c._position_registry().values():
        if reg.get("symbol") != sym or reg.get("tab") != tab:
            continue
        if pos_side and str(reg.get("position_side") or "").upper() != pos_side:
            continue
        if entry_time and reg.get("entry_time") and reg.get("entry_time") != entry_time:
            continue
        if reg.get("signal_sl") is not None or reg.get("placed_sl") is not None:
            return reg
    return None


async def _repair_history_sltp_diff_once():
    c = _core()
    """Backfill SL/TP diff on history from stored snapshots or position_registry."""
    if not c.state.get("history"):
        return
    repaired = 0
    for entry in c.state.get("history", []):
        if entry.get("sl_diff_pct") is not None and entry.get("tp_diff_pct") is not None:
            continue
        if entry.get("signal_sl") is None:
            reg = _registry_row_for_history(entry)
            if reg:
                for key in (
                    "signal_sl", "signal_tp", "signal_entry_price",
                    "placed_sl", "placed_tp", "entry_price", "side", "symbol",
                ):
                    if reg.get(key) is not None and entry.get(key) is None:
                        entry[key] = reg[key]
        diff = c._sltp_diff_fields_from_row(entry)
        if diff:
            entry.update(diff)
            repaired += 1
    if repaired:
        await c.save_state()
        print(f"[SL/TP Diff Repair] Enriched {repaired} history entries")


async def _repair_history_pnl_once():
    c = _core()
    """One-shot repair: re-fetch userTrades for saved history after bad income reconcile."""
    if not c.LIVE_MODE or not c.state.get("history"):
        return
    await asyncio.sleep(c.PNL_REPAIR_STARTUP_DELAY_SEC)
    await c._await_entry_window_clear("PnL Repair startup")
    if _binance_rate_limited():
        print("[PnL Repair] skipped — Binance rate limit active")
        return
    repaired = 0
    used_trade_ids: set[int] = set()
    batch_count = 0
    for entry in sorted(c.state.get("history", []), key=lambda h: h.get("exit_time") or ""):
        if c._entry_window_active():
            await c._await_entry_window_clear("PnL Repair")
        if _binance_rate_limited():
            print("[PnL Repair] paused — Binance rate limit active")
            break
        if entry.get("reason") == "ExchangeSync":
            continue
        sym = entry.get("symbol")
        tab = entry.get("tab")
        pos_side = entry.get("position_side")
        exit_time = entry.get("exit_time")
        if not sym or not tab or not pos_side or not exit_time:
            continue
        qty = _history_qty_for_entry(entry)
        order_id = entry.get("close_order_id")
        if order_id not in (None, "", 0):
            order_id = int(order_id)
        else:
            order_id = None
        summary = await _fetch_close_pnl_from_trades(
            sym,
            pos_side,
            entry.get("side") or ("Long" if pos_side == "LONG" else "Short"),
            qty,
            exit_time,
            order_id=order_id,
            client_order_id=entry.get("close_client_order_id"),
            exclude_trade_ids=used_trade_ids,
            tab=tab,
            ignore_entry_window=True,
        )
        if summary is None:
            await asyncio.sleep(c.PNL_REPAIR_ENTRY_DELAY_SEC)
            continue
        async with c._state_lock:
            target = _find_history_entry(sym, pos_side, tab, exit_time)
            if target is None:
                continue
            old_pnl = float(target.get("pnl_usd") or 0)
            if await _apply_history_pnl_update(
                target,
                summary,
                old_pnl,
                label=f"repair {sym} {tab}",
            ):
                repaired += 1
                used_trade_ids.update(int(tid) for tid in summary.get("trade_ids") or [])
        batch_count += 1
        await asyncio.sleep(c.PNL_REPAIR_ENTRY_DELAY_SEC)
        if batch_count >= c.PNL_REPAIR_BATCH_SIZE:
            batch_count = 0
            await asyncio.sleep(c.PNL_REPAIR_BATCH_PAUSE_SEC)
    if repaired:
        await c.save_state()
        print(f"[PnL Repair] Updated {repaired} history entries from userTrades")


async def close_position(pos_key, price, reason, skip_exchange=False, realized_pnl=None):
    c = _core()
    async with c._state_lock:
        await _close_position_unsafe(pos_key, price, reason, skip_exchange=skip_exchange, realized_pnl=realized_pnl)


def _is_percent_price_reject(exc: Exception):
    c = _core()
    text = c._http_error_text(exc) or str(exc)
    return "-4131" in text or "PERCENT_PRICE" in text or "percent price" in text.lower()


def _utc_now_ms():
    c = _core()
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _is_binance_rate_limit_text(text: str, http_status: int = 0):
    c = _core()
    lowered = (text or "").lower()
    return (
        http_status in (418, 429)
        or "418" in lowered
        or "-1003" in lowered
        or "too many requests" in lowered
        or "banned until" in lowered
    )


def _is_binance_rate_limit(exc: Exception):
    c = _core()
    status = int(getattr(getattr(exc, "response", None), "status_code", 0) or 0)
    text = c._http_error_text(exc) or str(exc)
    return _is_binance_rate_limit_text(text, status)


def _binance_rate_limit_snapshot():
    c = _core()
    active = _binance_rate_limited()
    until_ms = int(c._BINANCE_RATE_LIMIT_UNTIL_MS or 0)
    remaining_sec = max(0, (until_ms - _utc_now_ms()) // 1000) if active else 0
    until_utc = None
    if active and until_ms > 0:
        until_utc = datetime.fromtimestamp(
            until_ms / 1000, tz=timezone.utc,
        ).strftime("%Y-%m-%d %H:%M:%S UTC")
    return {
        "active": active,
        "until_ms": until_ms if active else None,
        "until_utc": until_utc,
        "remaining_sec": remaining_sec,
        "reason": (c._BINANCE_RATE_LIMIT_REASON or None) if active else None,
    }


def _schedule_binance_ban_alert(until_utc: str, remaining_sec: int):
    c = _core()
    if c._BINANCE_RATE_LIMIT_ALERT_SENT:
        return
    c._BINANCE_RATE_LIMIT_ALERT_SENT = True
    detail = c._BINANCE_RATE_LIMIT_REASON or "REST throttled"
    msg = (
        f"Binance API rate limit / IP ban until {until_utc} "
        f"({remaining_sec}s). New entries paused. {detail}"
    )
    print(f"[API Ban] {msg}")
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(
        c.record_error_event(msg, severity="warning", source="api_ban", notify=True),
    )


def _activate_binance_rate_limit(until_ms: int, detail: str):
    c = _core()
    until_ms = max(int(until_ms or 0), _utc_now_ms() + 1000)
    extended = until_ms > c._BINANCE_RATE_LIMIT_UNTIL_MS
    c._BINANCE_RATE_LIMIT_UNTIL_MS = max(c._BINANCE_RATE_LIMIT_UNTIL_MS, until_ms)
    c._BINANCE_RATE_LIMIT_REASON = str(detail or "").strip()[:240] or c._BINANCE_RATE_LIMIT_REASON
    if extended:
        snap = _binance_rate_limit_snapshot()
        until_utc = snap.get("until_utc") or "unknown"
        _schedule_binance_ban_alert(until_utc, int(snap.get("remaining_sec") or 0))


def _note_binance_rate_limit(
    exc: Exception | None = None,
    *,
    http_status: int = 0,
    body: str = "",
):
    c = _core()
    text = (body or (c._http_error_text(exc) if exc else "") or str(exc or "")).strip()
    status = http_status or int(getattr(getattr(exc, "response", None), "status_code", 0) or 0)
    if not _is_binance_rate_limit_text(text, status):
        return

    match = re.search(r"banned until (\d+)", text, re.IGNORECASE)
    if match:
        until_ms = int(match.group(1))
        detail = text[:240] if text else f"HTTP {status or 418}"
    else:
        until_ms = _utc_now_ms() + (c.BINANCE_IP_BAN_DEFAULT_BACKOFF_SEC * 1000)
        detail = text[:240] if text else f"HTTP {status or 418} (default backoff {c.BINANCE_IP_BAN_DEFAULT_BACKOFF_SEC}s)"
    _activate_binance_rate_limit(until_ms, detail)


def _binance_rate_limited():
    c = _core()
    if c._BINANCE_RATE_LIMIT_UNTIL_MS <= 0:
        return False
    now_ms = _utc_now_ms()
    if now_ms >= c._BINANCE_RATE_LIMIT_UNTIL_MS:
        c._BINANCE_RATE_LIMIT_UNTIL_MS = 0
        c._BINANCE_RATE_LIMIT_REASON = ""
        c._BINANCE_RATE_LIMIT_ALERT_SENT = False
        print("[API Ban] REST backoff ended — resuming normal polling")
        return False
    return True


async def _fetch_live_qty_cache():
    c = _core()
    """One-shot (symbol, positionSide) → abs qty snapshot for batch close/sync."""
    cache: dict[tuple[str, str], float] = {}
    try:
        exchange_pos = await binance_live.get_position_risk(c._http_client)
        for p in exchange_pos:
            qty = abs(float(p.get("positionAmt", 0) or 0))
            if qty <= 0:
                continue
            if not str(p.get("symbol", "")).endswith("USDT"):
                continue
            cache[c._position_tuple_from_exchange(p)] = qty
    except Exception as e:
        _note_binance_rate_limit(e)
        print(f"[Live] live qty cache fetch failed: {e}")
    return cache


def _cached_live_qty(
    sym: str,
    pos_side: str,
    live_qty_cache: dict[tuple[str, str], float] | None,
):
    c = _core()
    if live_qty_cache is None:
        return None
    return float(live_qty_cache.get((sym, str(pos_side).upper()), 0.0))


async def _resolve_live_qty(
    sym: str,
    pos_side: str,
    live_qty_cache: dict[tuple[str, str], float] | None = None,
):
    c = _core()
    """Return exchange qty for one hedge leg; prefer batch cache when provided."""
    cached = _cached_live_qty(sym, pos_side, live_qty_cache)
    if cached is not None:
        return cached, None
    try:
        return await c._live_position_qty(sym, pos_side), None
    except Exception as e:
        _note_binance_rate_limit(e)
        return None, e


async def _close_position_unsafe(
    pos_key,
    price,
    reason,
    skip_exchange=False,
    realized_pnl=None,
    fill_commission_usd=None,
    fill_fee_usd=None,
    close_order_id=None,
    live_qty_cache: dict[tuple[str, str], float] | None = None,
):
    c = _core()
    if pos_key not in c.state["open_positions"]: return
    pos = c.state["open_positions"][pos_key]
    tab = pos["tab"]
    sym = pos["symbol"]

    if tab not in c.state["balances"]:
        print(f"[WARN] close_position: unknown tab '{tab}' for {pos_key}, skipping")
        return

    close_qty = float(pos.get("qty") or 0)
    close_client_id = None

    if c.LIVE_MODE and not skip_exchange:
        # Invalidation or manual close: market-close first, then cancel the
        # specific SL/TP orders for THIS position only (not other strategies').
        pos_side = c._position_side_from_state(pos)
        close_side = "SELL" if pos["side"] == "Long" else "BUY"
        qty = float(pos.get("qty", c._effective_notional_size() / pos["entry_price"]) or 0)
        close_qty = qty
        close_order_id = None
        close_client_id = None
        close_res = None
        before_qty = None
        before_err = None
        verification_required = c._http_client is not None and hasattr(c._http_client, "get")
        before_qty, before_err = await _resolve_live_qty(sym, pos_side, live_qty_cache)
        if before_err is not None:
            print(f"[Live] pre-close qty check failed for {pos_key}: {before_err}")
        if before_qty is not None and before_qty <= 1e-9:
            print(f"[Live] {pos_key} has no live {sym} {pos_side} qty; removing stale c.state without market close")
        else:
            sibling_qty = sum(
                float(p.get("qty") or 0)
                for pk, p in c.state["open_positions"].items()
                if pk != pos_key
                and p.get("symbol") == sym
                and c._position_side_from_state(p) == pos_side
            )
            if before_qty is not None:
                max_close_qty = max(0.0, float(before_qty) - sibling_qty)
                if qty > max_close_qty + 1e-9:
                    capped_qty = binance_live.round_qty(sym, max_close_qty)
                    print(
                        f"[Live] capping {pos_key} close qty {qty} -> {capped_qty} "
                        f"(sibling_qty={sibling_qty:.8f}, live={before_qty:.8f})"
                    )
                    qty = capped_qty
                    close_qty = qty
            if qty <= 1e-9:
                print(
                    f"[Live] {pos_key} has no closeable qty after sibling cap "
                    f"(sibling_qty={sibling_qty:.8f}); removing stale c.state without market close"
                )
            else:
                try:
                    # In Hedge Mode: positionSide alone routes the close; reduceOnly not needed.
                    close_client_id = c._strategy_client_id(tab, pos_side, "CLOSE")
                    close_res = await binance_live.place_market_order(
                        c._http_client,
                        sym,
                        close_side,
                        qty,
                        position_side=pos_side,
                        client_order_id=close_client_id,
                    )
                    close_order_id = int(close_res.get("orderId") or 0) or None
                    c._record_bot_close_fill(sym, pos_side, close_side, qty, close_client_id)
                except Exception as e:
                    _note_binance_rate_limit(e)
                    if c.BINANCE_TESTNET and _is_percent_price_reject(e):
                        try:
                            close_client_id = c._strategy_client_id(tab, pos_side, "CLOSEPM")
                            close_res = await binance_live.place_price_match_ioc_order(
                                c._http_client,
                                sym,
                                close_side,
                                qty,
                                position_side=pos_side,
                                price_match="OPPONENT",
                                client_order_id=close_client_id,
                            )
                            close_order_id = int(close_res.get("orderId") or 0) or None
                            await asyncio.sleep(1.0)
                            c._record_bot_close_fill(sym, pos_side, close_side, qty, close_client_id)
                            print(f"[Live] market close percent-filter fallback used for {pos_key} via priceMatch=OPPONENT")
                        except Exception as pe:
                            _note_binance_rate_limit(pe)
                            after_fail_qty, _ = await _resolve_live_qty(sym, pos_side, live_qty_cache)
                            if after_fail_qty is not None and after_fail_qty <= 1e-9:
                                print(f"[Live] {pos_key} already flat after failed close; removing stale c.state")
                            else:
                                print(f"[Live] priceMatch close fallback FAILED for {sym}: {pe} — keeping position in c.state")
                                await c.send_telegram(
                                    f"⚠️ <b>ปิดออเดอร์ไม่สำเร็จ (Manual Close Failed)</b>\n"
                                    f"🧠 Strategy: <b>{tab} — {c._strategy_label(tab)}</b>\n"
                                    f"📌 Symbol: <b>{sym}</b> | Side: <b>{pos['side']}</b>\n"
                                    f"🔍 Error: <code>{pe}</code>\n"
                                    f"🛑 สถานะยังคงถูกติดตามในระบบ",
                                    is_error=True,
                                )
                                return
                    else:
                        after_fail_qty, _ = await _resolve_live_qty(sym, pos_side, live_qty_cache)
                        if after_fail_qty is not None and after_fail_qty <= 1e-9:
                            print(f"[Live] {pos_key} already flat on exchange; removing stale c.state (close error: {e})")
                        else:
                            print(f"[Live] market close FAILED for {sym}: {e} — keeping position in c.state")
                            await c.send_telegram(
                                f"⚠️ <b>ปิดออเดอร์ไม่สำเร็จ (Manual Close Failed)</b>\n"
                                f"🧠 Strategy: <b>{tab} — {c._strategy_label(tab)}</b>\n"
                                f"📌 Symbol: <b>{sym}</b> | Side: <b>{pos['side']}</b>\n"
                                f"🔍 Error: <code>{e}</code>\n"
                                f"🛑 สถานะยังคงถูกติดตามในระบบ",
                                is_error=True,
                            )
                            return
                after_qty = None
                if verification_required and close_res is not None:
                    after_qty, after_err = await _resolve_live_qty(sym, pos_side, live_qty_cache)
                    if after_err is not None:
                        print(f"[Live] post-close qty check failed for {pos_key}: {after_err}")

                executed_qty = float((close_res or {}).get("executedQty") or 0)
                target_close_qty = min(qty, before_qty) if before_qty is not None else qty
                if before_qty is not None and after_qty is not None:
                    closed_qty = max(0.0, before_qty - after_qty)
                    close_tol = max(target_close_qty * 0.001, 1e-9)
                    if closed_qty + close_tol < target_close_qty:
                        if closed_qty > close_tol:
                            remaining_qty = max(qty - closed_qty, 0.0)
                            pos["qty"] = binance_live.round_qty(sym, remaining_qty)
                            c._upsert_position_registry(pos_key, pos, status="partial_open")
                            await c.save_state()
                            msg = (
                                f"Partial close verification for {pos_key}: requested={qty}, "
                                f"closed={closed_qty}, remaining_state_qty={pos['qty']}, live_after={after_qty}"
                            )
                        else:
                            msg = (
                                f"Close verification failed for {pos_key}: live qty did not decrease "
                                f"(before={before_qty}, after={after_qty}, requested={qty})"
                            )
                        await c.record_error_event(msg, severity="warning", source="close_verify", notify=False)
                        print(f"[Live] {msg} — keeping position in c.state")
                        return
                elif executed_qty <= 0 and verification_required and close_res is not None:
                    if after_qty is not None and after_qty <= 1e-9:
                        print(f"[Live] post-close verify inconclusive but exchange flat; removing {pos_key}")
                    elif close_order_id:
                        print(f"[Live] close verify inconclusive for {pos_key}; accepting close order {close_order_id}")
                    else:
                        msg = f"Close verification inconclusive for {pos_key}: no executedQty and no fresh live qty"
                        await c.record_error_event(msg, severity="warning", source="close_verify", notify=False)
                        print(f"[Live] {msg} — keeping position in c.state")
                        return

        # Cancel only the SL/TP belonging to this specific position.
        for oid_key in ("sl_order_id", "tp_order_id"):
            oid = pos.get(oid_key)
            if oid:
                try:
                    await binance_live.cancel_algo_order(c._http_client, algo_id=oid)
                except Exception as e:
                    print(f"[Live] cancel {oid_key} {oid} for {pos_key}: {e}")

    c.state["open_positions"].pop(pos_key, None)
    c._mark_position_registry_closed(pos_key, reason)

    # Paper: STOP/TP market exits are taker with adverse slippage (like Binance conditional market).
    # Live PnL comes from exchange fills/income reconciliation.
    paper_mode = not c.LIVE_MODE
    if paper_mode:
        slippage = c.SLIPPAGE_PCT
        exit_fee_pct = c.EXIT_FEE_TAKER_PCT
    else:
        market_exit = reason not in ("TP", "TP_Gap")
        slippage = 0.0
        exit_fee_pct = c.EXIT_FEE_MAKER_PCT if not market_exit else c.EXIT_FEE_TAKER_PCT

    if pos["side"] == "Long":
        exit_price = price * (1 - slippage)
        gross_pnl = (exit_price - pos["entry_price"]) * close_qty
    else:
        exit_price = price * (1 + slippage)
        gross_pnl = (pos["entry_price"] - exit_price) * close_qty
    total_fee = (
        (pos["entry_price"] * close_qty) * c.ENTRY_FEE_PCT
        + (exit_price * close_qty) * exit_fee_pct
    ) if paper_mode else 0.0
    slippage_usd = abs(price - exit_price) * close_qty if paper_mode else 0.0

    fill_net = _fill_net_pnl(realized_pnl, fill_commission_usd)
    if fill_net is not None and c.LIVE_MODE:
        # WS fill: rp + USDT commission; BNB fees shown separately; reconcile refines ~6s.
        net_pnl = fill_net
        total_fee = (
            float(fill_fee_usd) if fill_fee_usd is not None
            else abs(float(fill_commission_usd or 0))
        )
        slippage_usd = 0.0
    else:
        net_pnl = gross_pnl - total_fee

    c.state["balances"][tab] += net_pnl
    exit_time_iso = c._utc_now_iso()
    history_row = {
        "tab": tab, "symbol": sym, "side": pos["side"],
        "position_side": c._position_side_from_state(pos),
        "entry_time": pos["entry_time"],
        "exit_time": exit_time_iso,
        "entry_price": pos["entry_price"],
        "exit_price": exit_price,
        "qty": float(close_qty),
        "close_order_id": close_order_id,
        "close_client_order_id": close_client_id,
        "pnl_usd": net_pnl, "fee_usd": total_fee,
        "slippage_usd": slippage_usd, "reason": reason,
        **c._position_sltp_diff_fields(pos),
    }
    if fill_net is not None and realized_pnl is not None:
        history_row["realized_only"] = float(realized_pnl)
    c.state["history"].append(history_row)

    if len(c.state["history"]) > c.HISTORY_CAP:
        c.state["history"] = c.state["history"][-c.HISTORY_CAP:]

    c._record_tab_stats_close(tab, net_pnl, entry=history_row)
    await c.save_state()

    pos_side = c._position_side_from_state(pos)
    _placed_sl = pos.get("placed_sl", pos.get("sl"))
    _placed_tp = pos.get("placed_tp", pos.get("tp"))
    placed_sl = float(_placed_sl) if _placed_sl is not None else None
    placed_tp = float(_placed_tp) if _placed_tp is not None else None

    if c.LIVE_MODE:
        import time as _time_mod
        c._last_live_close_mono = _time_mod.monotonic()
        asyncio.create_task(c._finalize_exit_notify(
            pos_key=pos_key,
            sym=sym,
            tab=tab,
            side=pos["side"],
            pos_side=pos_side,
            reason=reason,
            entry_time_iso=pos.get("entry_time"),
            exit_time_iso=exit_time_iso,
            entry_price=float(pos.get("entry_price") or 0),
            provisional_exit_price=float(exit_price),
            provisional_net_pnl=float(net_pnl),
            provisional_fee_usd=float(total_fee),
            close_qty=close_qty,
            close_order_id=close_order_id,
            placed_sl=placed_sl,
            placed_tp=placed_tp,
        ))
        asyncio.create_task(c.trigger_income_sync(8.0))
        if c.BINANCE_CLOSE_HISTORY_ENABLED:
            asyncio.create_task(c._refresh_binance_close_history(force=True))
        asyncio.create_task(c._sweep_algo_orders_for(sym, pos_side))
    else:
        c._log_exit_close(
            pos_key=pos_key,
            sym=sym,
            tab=tab,
            side=pos["side"],
            reason=reason,
            entry_time_iso=pos.get("entry_time"),
            exit_time_iso=exit_time_iso,
            entry_price=float(pos.get("entry_price") or 0),
            exit_price=float(exit_price),
            net_pnl=float(net_pnl),
            fee_usd=float(total_fee),
            placed_sl=placed_sl,
            placed_tp=placed_tp,
        )
        if net_pnl < 0:
            c._daily_loss_usd += abs(net_pnl)
            tripped = False
            if not c._circuit_breaker and c._daily_loss_usd >= c.CIRCUIT_BREAKER_DAILY_LOSS:
                c._circuit_breaker = True
                tripped = True
                print(f"[Circuit Breaker] ⛔ Daily loss limit ${c.CIRCUIT_BREAKER_DAILY_LOSS:.0f} reached "
                      f"(total loss today: ${c._daily_loss_usd:.2f}). Trading HALTED.")
            c._persist_circuit_breaker()
            await c.save_state()
            if tripped:
                await c.send_telegram(
                    f"⛔ <b>CIRCUIT BREAKER ACTIVATED</b>\n"
                    f"Daily loss: <b>${c._daily_loss_usd:.2f}</b> exceeded limit ${c.CIRCUIT_BREAKER_DAILY_LOSS:.0f}\n"
                    f"Trading halted until midnight UTC.",
                    is_error=True,
                )


from bot.feeds.klines import (
    get_klines,
)
