"""Exchange sync close backfill (extracted from bot.core)."""

from __future__ import annotations

from datetime import datetime, timezone

import binance_live

def _core():
    from bot import core
    return core


def _dt_to_ms(value: str | None):
    c = _core()
    if not value:
        return None
    try:
        raw = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            # Legacy rows used naive local wall time; normalize to UTC for Binance APIs.
            dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo).astimezone(timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def clear_sync_close_leg_cache():
    c = _core()
    c._sync_close_leg_cache.clear()


def _leg_key(pos: dict) -> tuple[str, str]:
    c = _core()
    return (pos["symbol"], c._position_side_from_state(pos))


def _sibling_positions_on_leg(pos: dict) -> list[dict]:
    c = _core()
    sym, pos_side = _leg_key(pos)
    return [
        p
        for p in c.state["open_positions"].values()
        if p.get("symbol") == sym and c._position_side_from_state(p) == pos_side
    ]


def _group_qty_for_leg(pos: dict) -> float:
    siblings = _sibling_positions_on_leg(pos)
    total = sum(float(p.get("qty") or 0) for p in siblings)
    if total > 0:
        return total
    return max(float(pos.get("qty") or 0), 0.0)


def _history_has_sync_close(pos: dict):
    c = _core()
    return any(
        h.get("symbol") == pos.get("symbol")
        and str(h.get("position_side") or c._position_side_name(h.get("side"))).upper()
            == c._position_side_from_state(pos)
        and h.get("entry_time") == pos.get("entry_time")
        and h.get("reason") == "ExchangeSync"
        for h in c.state.get("history", [])
    )


def _close_trades_for_pos(pos: dict, trades: list[dict]) -> list[dict]:
    c = _core()
    close_side = "SELL" if pos["side"] == "Long" else "BUY"
    pos_side = c._position_side_from_state(pos)
    close_trades = []
    for trade in trades:
        trade_pos_side = str(trade.get("positionSide") or "").upper()
        if trade.get("side") != close_side:
            continue
        if trade_pos_side and trade_pos_side != pos_side:
            continue
        if float(trade.get("realizedPnl", 0) or 0) == 0:
            continue
        close_trades.append(trade)
    return close_trades


def _leg_close_totals(close_trades: list[dict], *, fallback_entry_price: float) -> dict:
    c = _core()
    qty_sum = sum(float(t.get("qty", 0) or 0) for t in close_trades)
    quote_sum = sum(float(t.get("quoteQty", 0) or 0) for t in close_trades)
    exit_price = (quote_sum / qty_sum) if qty_sum else float(close_trades[-1].get("price", fallback_entry_price))
    realized = sum(float(t.get("realizedPnl", 0) or 0) for t in close_trades)
    usdt_commission, fee_usd = c._sum_trades_commission_parts(close_trades)
    return {
        "exit_price": exit_price,
        "net_pnl": realized + usdt_commission,
        "fee_usd": fee_usd,
    }


async def _leg_sync_close_snapshot(pos: dict) -> dict | None:
    c = _core()
    leg_key = _leg_key(pos)
    cached = c._sync_close_leg_cache.get(leg_key)
    if cached is not None:
        return cached

    sym = pos["symbol"]
    start_ms = _dt_to_ms(pos.get("entry_time"))
    try:
        trades = await binance_live.get_account_trades(
            c._http_client,
            sym,
            start_time=start_ms,
            end_time=int(datetime.now(timezone.utc).timestamp() * 1000),
        )
    except Exception as e:
        print(f"[Live Sync] Could not fetch trade history for stale {sym}: {e}")
        trades = []

    close_trades = _close_trades_for_pos(pos, trades)
    if not close_trades:
        return None

    totals = _leg_close_totals(close_trades, fallback_entry_price=float(pos["entry_price"]))
    snapshot = {
        **totals,
        "group_qty": _group_qty_for_leg(pos),
    }
    c._sync_close_leg_cache[leg_key] = snapshot
    return snapshot


def _tab_share_from_snapshot(pos: dict, snapshot: dict) -> float:
    pos_qty = max(float(pos.get("qty") or 0), 0.0)
    group_qty = max(float(snapshot.get("group_qty") or 0), 0.0)
    if group_qty <= 0:
        return 1.0
    return min(1.0, pos_qty / group_qty)


async def record_exchange_sync_close(pos: dict):
    c = _core()
    """Best-effort history backfill for positions closed while the bot was offline."""
    if _history_has_sync_close(pos):
        return

    sym = pos["symbol"]
    tab = pos["tab"]
    pos_side = c._position_side_from_state(pos)
    pos_qty = max(float(pos.get("qty") or 0), 0.0)
    snapshot = await _leg_sync_close_snapshot(pos)
    if snapshot is None:
        print(
            f"[Live Sync] Stale {sym} {pos_side} removed, "
            "but no realized close trades were found to backfill PnL"
        )
        return

    share = _tab_share_from_snapshot(pos, snapshot)
    net_pnl = snapshot["net_pnl"] * share
    fee_usd = snapshot["fee_usd"] * share
    exit_price = snapshot["exit_price"]

    if tab in c.state["balances"]:
        c.state["balances"][tab] += net_pnl
    c.state["history"].append({
        "tab": tab, "symbol": sym, "side": pos["side"], "position_side": pos_side,
        "entry_time": pos.get("entry_time"),
        "exit_time": c._utc_now_iso(),
        "entry_price": pos["entry_price"],
        "exit_price": exit_price,
        "qty": pos_qty,
        "pnl_usd": net_pnl,
        "fee_usd": fee_usd,
        "reason": "ExchangeSync",
    })
    if len(c.state["history"]) > c.HISTORY_CAP:
        c.state["history"] = c.state["history"][-c.HISTORY_CAP:]

    c._record_tab_stats_close(tab, net_pnl, entry={"symbol": sym, "side": pos["side"], "pnl_usd": net_pnl})

    if c.LIVE_MODE and net_pnl < 0:
        c._daily_loss_usd += abs(net_pnl)
        if c._daily_loss_usd >= c.CIRCUIT_BREAKER_DAILY_LOSS:
            c._circuit_breaker = True
        c._persist_circuit_breaker()
    leg_total = snapshot["net_pnl"]
    if share < 0.9999:
        print(
            f"[Live Sync] Backfilled stale close {sym} {tab}: net PnL ${net_pnl:.2f} "
            f"({share * 100:.1f}% of leg ${leg_total:.2f}, qty={pos_qty:.4f})"
        )
    else:
        print(f"[Live Sync] Backfilled stale close {sym} {tab}: net PnL ${net_pnl:.2f}")
