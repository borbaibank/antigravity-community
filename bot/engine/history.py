"""Trade history, SL/TP diff helpers, and dashboard history (extracted from bot.core)."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import binance_live
from config import (
    BINANCE_CLOSE_HISTORY_BATCH_SIZE,
    BINANCE_CLOSE_HISTORY_DAYS,
    BINANCE_CLOSE_HISTORY_REFRESH_SYMBOL_CAP,
    BINANCE_CLOSE_HISTORY_SYMBOL_DELAY_SEC,
    BINANCE_CLOSE_HISTORY_TTL_SEC,
    DASHBOARD_WS_HISTORY_LIMIT,
    DASHBOARD_EQUITY_CURVE_MAX_POINTS,
    HISTORY_CAP,
    UDS_ACCOUNT_FRESH_SEC,
)

def _core():
    from bot import core
    return core


def _sltp_diff_pct_from_entry(
    sym: str,
    side: str,
    entry_px: float,
    actual_sl: float,
    actual_tp: float,
    signal_sl: float,
    signal_tp: float,
    signal_entry_px: float | None = None,
):
    c = _core()
    """Actual vs strategy SL/TP re-anchored at fill, as % of entry (signed price delta)."""
    entry_px = float(entry_px or 0)
    if entry_px <= 0:
        return None, None
    signal_ep = float(signal_entry_px if signal_entry_px is not None else entry_px)
    strat_sl, strat_tp = c._protection_prices_from_entry(
        sym,
        side,
        entry_px,
        float(signal_sl),
        float(signal_tp),
        signal_ep,
    )
    sl_diff = (float(actual_sl) - strat_sl) / entry_px * 100.0
    tp_diff = (float(actual_tp) - strat_tp) / entry_px * 100.0
    return round(sl_diff, 4), round(tp_diff, 4)


def _position_sltp_snapshot_fields(pos: dict):
    c = _core()
    """Persist strategy vs placed SL/TP on history/registry rows."""
    out: dict = {}
    for key in ("signal_sl", "signal_tp", "signal_entry_price"):
        val = pos.get(key)
        if val is not None:
            out[key] = float(val)
    placed_sl = pos.get("placed_sl", pos.get("sl"))
    placed_tp = pos.get("placed_tp", pos.get("tp"))
    if placed_sl is not None:
        out["placed_sl"] = float(placed_sl)
    if placed_tp is not None:
        out["placed_tp"] = float(placed_tp)
    return out


def _sltp_diff_fields_from_row(row: dict):
    c = _core()
    """sl_diff_pct / tp_diff_pct vs strategy re-anchored at entry (position or history)."""
    if row.get("sl_diff_pct") is not None or row.get("tp_diff_pct") is not None:
        sl_pct = row.get("sl_diff_pct")
        tp_pct = row.get("tp_diff_pct")
        out: dict = {}
        if sl_pct is not None:
            out["sl_diff_pct"] = float(sl_pct)
        if tp_pct is not None:
            out["tp_diff_pct"] = float(tp_pct)
        return out

    entry_px = float(row.get("entry_price") or 0)
    if entry_px <= 0:
        return {}
    signal_sl = row.get("signal_sl")
    signal_tp = row.get("signal_tp")
    if signal_sl is None or signal_tp is None:
        return {}
    placed_sl = row.get("placed_sl", row.get("sl"))
    placed_tp = row.get("placed_tp", row.get("tp"))
    if placed_sl is None or placed_tp is None:
        return {}

    sym = str(row.get("symbol") or "")
    side = str(row.get("side") or "")
    sl_pct, tp_pct = c._sltp_diff_pct_from_entry(
        sym,
        side,
        entry_px,
        float(placed_sl),
        float(placed_tp),
        float(signal_sl),
        float(signal_tp),
        row.get("signal_entry_price"),
    )
    out = {}
    if sl_pct is not None:
        out["sl_diff_pct"] = sl_pct
    if tp_pct is not None:
        out["tp_diff_pct"] = tp_pct
    return out


def _position_sltp_diff_fields(pos: dict):
    c = _core()
    """History payload: snapshot + diff vs strategy at actual entry."""
    return {**_position_sltp_snapshot_fields(pos), **_sltp_diff_fields_from_row(pos)}


def _enrich_history_entry(entry: dict):
    c = _core()
    """Ensure dashboard/history rows expose SL/TP diff (compute if snapshot exists)."""
    out = dict(entry)
    out.update(_sltp_diff_fields_from_row(out))
    return out


def _ms_to_iso(ms: int):
    c = _core()
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()


def _estimate_entry_from_close(side: str, exit_px: float, realized_pnl: float, qty: float):
    c = _core()
    """Approximate entry from close fill (realizedPnl before commission)."""
    if qty <= 0 or exit_px <= 0:
        return 0.0
    if str(side).lower() == "long":
        return exit_px - (realized_pnl / qty)
    return exit_px + (realized_pnl / qty)


def _bot_side_from_binance_trade(pos_side: str, trade_side: str):
    c = _core()
    ps = str(pos_side or "").upper()
    ts = str(trade_side or "").upper()
    if ps == "LONG":
        return "Long"
    if ps == "SHORT":
        return "Short"
    return "Long" if ts == "SELL" else "Short"


def _symbols_for_trade_history(*, full_cap: bool = False):
    c = _core()
    """Symbols for userTrades pull — prioritize open/recent; background uses smaller cap."""
    syms: set[str] = set()
    for pos in c.state.get("open_positions", {}).values():
        sym = pos.get("symbol")
        if sym:
            syms.add(str(sym))
    for pos in c.exchange_account.get("positions") or []:
        sym = pos.get("symbol")
        if sym:
            syms.add(str(sym))
    for entry in c.state.get("history", [])[-40:]:
        sym = entry.get("symbol")
        if sym:
            syms.add(str(sym))
    for sym in c.SCAN_SYMBOLS[:20]:
        syms.add(sym)
    cap = c.BINANCE_CLOSE_HISTORY_SYMBOL_CAP if full_cap else c.BINANCE_CLOSE_HISTORY_REFRESH_SYMBOL_CAP
    return sorted(syms)[:cap]


async def _fetch_symbol_user_trades(symbol: str, start_ms: int):
    c = _core()
    """Paginate /fapi/v1/userTrades so busy symbols are not truncated at 1000 rows."""
    if c._http_client is None:
        return []
    sym = str(symbol or "").strip().upper()
    if not sym:
        return []
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    cursor = int(start_ms)
    out: list = []
    while cursor < end_ms:
        batch = await binance_live.get_account_trades(
            c._http_client,
            sym,
            start_time=cursor,
            end_time=end_ms,
            limit=1000,
        )
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 1000:
            break
        cursor = int(batch[-1].get("time") or cursor) + 1
    return out


def _binance_trade_to_api_row(t: dict):
    c = _core()
    return {
        "symbol": t.get("symbol"),
        "side": t.get("side"),
        "positionSide": t.get("positionSide"),
        "price": float(t.get("price", 0)),
        "qty": float(t.get("qty", 0)),
        "realizedPnl": float(t.get("realizedPnl", 0)),
        "commission": float(t.get("commission", 0)),
        "commissionAsset": t.get("commissionAsset"),
        "time": t.get("time"),
        "buyer": t.get("buyer"),
        "maker": t.get("maker"),
        "orderId": t.get("orderId"),
        "tradeId": t.get("id"),
    }


def _resolve_symbol_leverage(sym: str, existing: dict | None = None):
    c = _core()
    """Leverage for dashboard rows — UDS updates omit it; prefer REST/cache/config."""
    if existing:
        lev = int(existing.get("leverage") or 0)
        if lev > 0:
            return lev
    return int(c._symbol_leverage.get(sym) or c._effective_leverage())


async def _ensure_symbol_leverage(sym: str):
    c = _core()
    """Set Binance leverage for symbol and cache the confirmed value."""
    target = c._effective_leverage()
    res = await binance_live.set_leverage(c._http_client, sym, target)
    lev = int(res.get("leverage") or target)
    c._symbol_leverage[sym] = lev
    return lev


def _uds_position_row(p: dict, existing: dict | None = None):
    c = _core()
    """Map futures ACCOUNT_UPDATE position object to dashboard exchange row."""
    sym = str(p.get("s") or "")
    if not sym:
        return None
    ps = str(p.get("ps") or "BOTH").upper()
    try:
        amt = float(p.get("pa") or 0)
    except (TypeError, ValueError):
        amt = 0.0
    if ps == "SHORT":
        signed = -abs(amt)
    elif ps == "LONG":
        signed = abs(amt)
    else:
        signed = amt
    if abs(signed) < 1e-12:
        return None
    ep = float(p.get("ep") or 0)
    up = float(p.get("up") or 0)
    mark = float(p.get("mp") or 0)
    if mark <= 0 and existing:
        mark = float(existing.get("markPrice") or 0)
    liq = float(p.get("lp") or 0)
    if liq <= 0 and existing:
        liq = float(existing.get("liquidationPrice") or 0)
    bep = float(p.get("bep") or 0) or ep
    if bep <= 0 and existing:
        bep = float(existing.get("breakEvenPrice") or ep)
    return {
        "symbol": sym,
        "side": "Long" if signed > 0 else "Short",
        "positionAmt": signed,
        "entryPrice": ep,
        "breakEvenPrice": bep,
        "markPrice": mark,
        "unrealizedProfit": up,
        "liquidationPrice": liq,
        "leverage": _resolve_symbol_leverage(sym, existing),
        "marginType": str(p.get("mt") or "cross").lower(),
        "isolatedWallet": float(p.get("iw") or 0),
        "isolatedMargin": float(existing.get("isolatedMargin") or 0) if existing else 0.0,
        "notional": abs(signed) * ep if ep else 0.0,
        "maintMargin": float(existing.get("maintMargin") or 0) if existing else 0.0,
    }


def _apply_uds_account_update(event: dict):
    c = _core()
    """Merge Binance futures ACCOUNT_UPDATE into c.exchange_account (reduces REST polling)."""
    acct = event.get("a")
    if not isinstance(acct, dict):
        return
    import time as _time_mod
    c._last_uds_account_update_mono = _time_mod.monotonic()

    ea = dict(c.exchange_account) if c.exchange_account else {
        "walletBalance": 0.0,
        "availableBalance": 0.0,
        "unrealizedProfit": 0.0,
        "marginBalance": 0.0,
        "positions": [],
        "low_margin_alert": False,
    }
    positions = list(ea.get("positions") or [])
    pos_index: dict[tuple[str, str], int] = {}
    for i, row in enumerate(positions):
        sym = str(row.get("symbol") or "")
        side = str(row.get("side") or "")
        if sym:
            pos_index[(sym, side)] = i

    for bal in acct.get("B") or []:
        if str(bal.get("a") or "") != "USDT":
            continue
        wb = float(bal.get("wb") or ea.get("walletBalance") or 0)
        cw = float(bal.get("cw") or wb)
        ea["walletBalance"] = wb
        ea["availableBalance"] = cw
        ea["marginBalance"] = wb

    for p in acct.get("P") or []:
        sym = str(p.get("s") or "")
        ps = str(p.get("ps") or "BOTH").upper()
        try:
            amt = float(p.get("pa") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        if ps == "SHORT":
            signed = -abs(amt)
        elif ps == "LONG":
            signed = abs(amt)
        else:
            signed = amt
        side_name = "Long" if signed > 0 else "Short"
        key = (sym, side_name)
        existing = positions[pos_index[key]] if key in pos_index else None
        row = _uds_position_row(p, existing)
        if row is None:
            if key in pos_index:
                del positions[pos_index[key]]
                pos_index = {
                    (str(r.get("symbol") or ""), str(r.get("side") or "")): i
                    for i, r in enumerate(positions)
                }
            continue
        if key in pos_index:
            positions[pos_index[key]] = {**existing, **row}
        else:
            pos_index[key] = len(positions)
            positions.append(row)

    open_pos = [p for p in positions if abs(float(p.get("positionAmt", 0) or 0)) > 0]
    ea["positions"] = open_pos
    ea["unrealizedProfit"] = sum(float(p.get("unrealizedProfit", 0) or 0) for p in open_pos)
    avail = float(ea.get("availableBalance", 0) or 0)
    ea["low_margin_alert"] = avail < c.LOW_MARGIN_THRESHOLD
    c.exchange_account = ea
    c._last_exchange_account_ok_at = c._utc_now_iso()


def _aggregate_binance_close_rows(trades: list):
    c = _core()
    """Group userTrades closing fills by (symbol, positionSide, orderId)."""
    groups: dict[tuple, list] = {}
    for trade in trades:
        realized = float(trade.get("realizedPnl") or 0)
        commission = float(trade.get("commission") or 0)
        if abs(realized) < 1e-12 and abs(commission) < 1e-12:
            continue
        sym = str(trade.get("symbol") or "")
        if not sym:
            continue
        pos_side = str(trade.get("positionSide") or "BOTH").upper()
        order_id = int(trade.get("orderId") or 0)
        key = (sym, pos_side, order_id)
        groups.setdefault(key, []).append(trade)

    rows: list[dict] = []
    for (sym, pos_side, order_id), group in groups.items():
        realized = sum(float(t.get("realizedPnl") or 0) for t in group)
        usdt_commission, fee_usd = c._sum_trades_commission_parts(group)
        qty = sum(float(t.get("qty") or 0) for t in group)
        quote = sum(float(t.get("quoteQty") or 0) for t in group)
        exit_px = (quote / qty) if qty > 0 else float(group[-1].get("price") or 0)
        exit_ms = max(int(t.get("time") or 0) for t in group)
        trade_side = str(group[0].get("side") or "")
        bot_side = _bot_side_from_binance_trade(pos_side, trade_side)
        entry_px = _estimate_entry_from_close(bot_side, exit_px, realized, qty)
        rows.append({
            "symbol": sym,
            "side": bot_side,
            "position_side": pos_side,
            "exit_time": _ms_to_iso(exit_ms),
            "entry_price": entry_px,
            "exit_price": exit_px,
            "qty": qty,
            "pnl_usd": realized + usdt_commission,
            "fee_usd": fee_usd,
            "realized_only": realized,
            "close_order_id": order_id or None,
            "reason": "Binance",
            "source": "binance",
        })
    rows.sort(key=lambda r: r.get("exit_time") or "", reverse=True)
    return rows


def _merge_bot_metadata_into_binance_close(row: dict):
    c = _core()
    """Attach tab/reason/entry/SL-TP diff from bot history when the close matches."""
    sym = row.get("symbol")
    exit_ms = c._dt_to_ms(row.get("exit_time"))
    pos_side = str(row.get("position_side") or "").upper()
    close_oid = row.get("close_order_id")

    best = None
    best_delta = None
    for entry in reversed(c.state.get("history", [])):
        if entry.get("symbol") != sym:
            continue
        if pos_side and str(entry.get("position_side") or "").upper() != pos_side:
            continue
        if close_oid and entry.get("close_order_id"):
            try:
                if int(entry.get("close_order_id")) == int(close_oid):
                    best = entry
                    break
            except (TypeError, ValueError):
                pass
        entry_ms = c._dt_to_ms(entry.get("exit_time"))
        if exit_ms is None or entry_ms is None:
            continue
        delta = abs(entry_ms - exit_ms)
        if delta > 120_000:
            continue
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best = entry

    out = dict(row)
    if best:
        out["tab"] = best.get("tab") or "Recovered"
        out["reason"] = best.get("reason") or out.get("reason") or "Binance"
        if float(best.get("entry_price") or 0) > 0:
            out["entry_price"] = float(best["entry_price"])
        if float(best.get("exit_price") or 0) > 0:
            out["exit_price"] = float(best["exit_price"])
        for key in (
            "signal_sl", "signal_tp", "signal_entry_price",
            "placed_sl", "placed_tp", "sl_diff_pct", "tp_diff_pct",
        ):
            if best.get(key) is not None:
                out[key] = best[key]
    else:
        out["tab"] = c._attribute_income_to_tab(
            str(sym or ""),
            exit_ms or 0,
            position_side=pos_side,
        )
    return _enrich_history_entry(out)


def _rebuild_binance_history_caches():
    c = _core()
    """Rebuild close/raw trade caches from per-symbol userTrades cache."""
    all_trades: list = []
    for sym_trades in c._BINANCE_TRADES_BY_SYMBOL.values():
        all_trades.extend(sym_trades or [])
    api_rows = [_binance_trade_to_api_row(t) for t in all_trades]
    api_rows.sort(key=lambda x: x.get("time") or 0, reverse=True)
    c._BINANCE_RAW_TRADES_CACHE = api_rows
    rows = [
        _merge_bot_metadata_into_binance_close(r)
        for r in _aggregate_binance_close_rows(all_trades)
    ]
    c._BINANCE_CLOSE_HISTORY_CACHE = rows
    if c.LIVE_MODE:
        c._rebuild_tab_stats_from_binance_closes()
    return len(rows)


def _next_trade_history_batch(*, force: bool = False):
    c = _core()
    """Round-robin slice of prioritized symbols for incremental userTrades pulls."""
    all_symbols = _symbols_for_trade_history(full_cap=True)
    if not all_symbols:
        return []
    batch_size = c.BINANCE_CLOSE_HISTORY_BATCH_SIZE
    if force:
        batch_size = min(
            len(all_symbols),
            max(c.BINANCE_CLOSE_HISTORY_BATCH_SIZE, c.BINANCE_CLOSE_HISTORY_REFRESH_SYMBOL_CAP // 3),
        )
    batch_size = min(batch_size, len(all_symbols))
    start = c._BINANCE_HISTORY_SYMBOL_INDEX % len(all_symbols)
    batch = [all_symbols[(start + i) % len(all_symbols)] for i in range(batch_size)]
    c._BINANCE_HISTORY_SYMBOL_INDEX = (start + batch_size) % len(all_symbols)
    return batch


async def _refresh_binance_close_history(force: bool = False):
    c = _core()
    """Pull closing fills from Binance userTrades (round-robin batches) and cache."""
    if not c._binance_close_history_enabled() or c._http_client is None:
        return 0
    if c._binance_rate_limited():
        return len(c._BINANCE_CLOSE_HISTORY_CACHE)
    if not force and c._entry_window_active():
        return len(c._BINANCE_CLOSE_HISTORY_CACHE)
    import time as _time_mod
    now = _time_mod.monotonic()
    if (
        not force
        and c._BINANCE_CLOSE_HISTORY_CACHE
        and (now - c._BINANCE_CLOSE_HISTORY_FETCHED_AT) < c.BINANCE_CLOSE_HISTORY_TTL_SEC
    ):
        return len(c._BINANCE_CLOSE_HISTORY_CACHE)
    if c._BINANCE_CLOSE_HISTORY_LOCK.locked() and not force:
        return len(c._BINANCE_CLOSE_HISTORY_CACHE)

    async with c._BINANCE_CLOSE_HISTORY_LOCK:
        now = _time_mod.monotonic()
        if (
            not force
            and c._BINANCE_CLOSE_HISTORY_CACHE
            and (now - c._BINANCE_CLOSE_HISTORY_FETCHED_AT) < c.BINANCE_CLOSE_HISTORY_TTL_SEC
        ):
            return len(c._BINANCE_CLOSE_HISTORY_CACHE)

        start_ms = int(
            (datetime.now(timezone.utc) - timedelta(days=c.BINANCE_CLOSE_HISTORY_DAYS)).timestamp() * 1000
        )
        symbols = _next_trade_history_batch(force=force)
        if not symbols:
            return 0

        fetched = 0
        for sym in symbols:
            if c._binance_rate_limited():
                break
            try:
                batch = await _fetch_symbol_user_trades(sym, start_ms)
                c._BINANCE_TRADES_BY_SYMBOL[sym] = batch
                fetched += 1
            except Exception as e:
                print(f"[Binance History] userTrades {sym}: {e}")
                c._note_binance_rate_limit(e)
            await asyncio.sleep(c.BINANCE_CLOSE_HISTORY_SYMBOL_DELAY_SEC)

        if fetched == 0:
            return len(c._BINANCE_CLOSE_HISTORY_CACHE)

        row_count = _rebuild_binance_history_caches()
        c._BINANCE_CLOSE_HISTORY_FETCHED_AT = now
        print(
            f"[Binance History] Cached {row_count} closes from userTrades "
            f"(batch {fetched}/{len(symbols)} symbols, ttl={c.BINANCE_CLOSE_HISTORY_TTL_SEC}s)"
        )
        return row_count


async def binance_close_history_loop():
    c = _core()
    """Background refresh for dashboard Recent Trades (LIVE)."""
    if not c._binance_close_history_enabled():
        return
    await asyncio.sleep(10)
    while True:
        try:
            if not c._binance_rate_limited():
                await _refresh_binance_close_history()
        except Exception as e:
            print(f"[Binance History] refresh error: {e}")
        await asyncio.sleep(c.BINANCE_CLOSE_HISTORY_TTL_SEC)


def _history_source_rows():
    c = _core()
    if c._use_binance_close_cache():
        return list(c._BINANCE_CLOSE_HISTORY_CACHE)
    return list(c.state.get("history", []))


def _filter_history_rows(
    rows,
    *,
    tab: str | None = None,
    days: int = 0,
    use_config_days: bool = False,
):
    c = _core()
    if use_config_days:
        days = int(c.BINANCE_CLOSE_HISTORY_DAYS or 0)
    if days > 0:
        cutoff_ms = int(
            (datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000
        )
        rows = [
            r for r in rows
            if (c._dt_to_ms(r.get("exit_time")) or 0) >= cutoff_ms
        ]
    if tab and tab not in ("All", ""):
        rows = [r for r in rows if (r.get("tab") or "Recovered") == tab]
    rows.sort(key=lambda r: c._dt_to_ms(r.get("exit_time")) or 0, reverse=True)
    return rows


def _dashboard_ws_history_rows():
    """Closed-trade rows for dashboard WS (window + cap — keeps frames under WS size limits)."""
    c = _core()
    rows = _filter_history_rows(_history_source_rows(), use_config_days=True)
    limit = int(c.DASHBOARD_WS_HISTORY_LIMIT or 0)
    if limit > 0 and len(rows) > limit:
        rows = rows[:limit]
    return rows


def _paginated_history_page(
    *,
    tab: str | None = None,
    offset: int = 0,
    limit: int = 50,
    days: int = 0,
):
    """Paginated close history for GET /api/history (newest first; days=0 = all time in state)."""
    c = _core()
    rows = _filter_history_rows(_history_source_rows(), tab=tab, days=days)
    total = len(rows)
    offset = max(0, int(offset))
    page_max = max(1, int(getattr(c, "DASHBOARD_HISTORY_PAGE_MAX", 200) or 200))
    limit = max(1, min(int(limit), page_max))
    page_rows = rows[offset: offset + limit]
    return {
        "history": [_enrich_history_entry(h) for h in page_rows],
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < total,
        "source": _dashboard_recent_history_source(),
        "tab": tab or "All",
        "days": int(days),
    }


def _dashboard_binance_recent_history():
    c = _core()
    return list(c._BINANCE_CLOSE_HISTORY_CACHE)


def _dashboard_recent_history_source():
    c = _core()
    if c._use_binance_close_cache():
        return "binance"
    return "bot"


def _dashboard_recent_history():
    c = _core()
    return [_enrich_history_entry(h) for h in _dashboard_ws_history_rows()]


def _dashboard_stats_trade_history():
    c = _core()
    """Closed trades for win-rate / charts — bot history unless bulk userTrades cache is on."""
    return [_enrich_history_entry(h) for h in _dashboard_ws_history_rows()]


def _history_rows_as_api_trades(limit: int = 50):
    c = _core()
    """Map bot close history to /api/trades shape when bulk userTrades REST is disabled."""
    rows: list[dict] = []
    for entry in reversed(c.state.get("history", [])):
        exit_ms = c._dt_to_ms(entry.get("exit_time"))
        side = str(entry.get("side") or "")
        pos_side = str(entry.get("position_side") or "").upper()
        if not pos_side:
            pos_side = "LONG" if side == "Long" else "SHORT"
        rows.append({
            "symbol": entry.get("symbol"),
            "side": "BUY" if side == "Long" else "SELL",
            "positionSide": pos_side,
            "price": float(entry.get("exit_price") or 0),
            "qty": float(entry.get("qty") or 0),
            "realizedPnl": float(entry.get("realized_only", entry.get("pnl_usd")) or 0),
            "commission": float(entry.get("fee_usd") or 0),
            "commissionAsset": "USDT",
            "time": exit_ms,
            "orderId": entry.get("close_order_id"),
        })
        if len(rows) >= limit:
            break
    return rows


def _dashboard_equity_close_series(tab_filter: str | None = None):
    c = _core()
    """Cumulative realized curve from close rows (time-sorted)."""
    rows = list(_dashboard_ws_history_rows())
    if tab_filter and tab_filter not in ("All", ""):
        rows = [r for r in rows if (r.get("tab") or "Recovered") == tab_filter]
    return _equity_close_series_from_rows(rows)


def _equity_close_series_from_rows(rows):
    c = _core()
    rows = sorted(rows, key=lambda r: c._dt_to_ms(r.get("exit_time")) or 0)
    series: list[dict] = [{"exit_time": None, "cumulative": 0.0, "symbol": "", "pnl_usd": 0.0}]
    cumulative = 0.0
    for row in rows:
        pnl = float(row.get("pnl_usd") or 0)
        cumulative += pnl
        series.append({
            "exit_time": row.get("exit_time"),
            "cumulative": cumulative,
            "symbol": row.get("symbol") or "",
            "pnl_usd": pnl,
            "tab": row.get("tab"),
        })
    return series


def _downsample_equity_series(series: list, max_points: int) -> list:
    if max_points <= 0 or len(series) <= max_points:
        return series
    if max_points < 2:
        return [series[0], series[-1]]
    out = [series[0]]
    inner = series[1:-1]
    if not inner:
        out.append(series[-1])
        return out
    middle_budget = max_points - 2
    step = len(inner) / middle_budget
    last_idx = -1
    for i in range(middle_budget):
        idx = min(len(inner) - 1, int(i * step))
        if idx != last_idx:
            out.append(inner[idx])
            last_idx = idx
    if out[-1] is not series[-1]:
        out.append(series[-1])
    return out


def _enabled_dashboard_tabs() -> list[str]:
    c = _core()
    te = c.state.get("tab_enabled") or {}
    return [tab for tab in c.TABS if te.get(tab)]


def _filter_history_rows_for_equity_curve(rows, tab: str | None):
    """Dashboard curve: single tab, or All = enabled tabs only (matches static/index.html)."""
    if tab and tab not in ("All", ""):
        return [r for r in rows if (r.get("tab") or "Recovered") == tab]
    enabled = set(_enabled_dashboard_tabs())
    if not enabled:
        return []
    return [r for r in rows if (r.get("tab") or "Recovered") in enabled]


def _max_drawdown_from_equity_values(values: list[float]) -> dict | None:
    if len(values) < 2:
        return None
    peak = float(values[0])
    max_dd_usd = 0.0
    max_dd_pct = 0.0
    for value in values:
        v = float(value)
        if v > peak:
            peak = v
        dd_usd = peak - v
        if dd_usd > max_dd_usd:
            max_dd_usd = dd_usd
            max_dd_pct = (dd_usd / peak * 100.0) if peak > 0 else 0.0
    return {
        "pct": round(max_dd_pct, 4),
        "usd": round(max_dd_usd, 6),
        "peak": round(peak, 6),
    }


def _dashboard_equity_values_from_series(series: list[dict], *, tab: str | None) -> list[float]:
    """Equity levels for risk metrics — mirrors dashboard closeEquityForDashboardRange (without Now point)."""
    c = _core()
    paper_ledger = not c.LIVE_MODE and tab and tab not in ("All", "")
    if paper_ledger:
        base = float(c.INITIAL_BALANCE or 7000)
        values = [base]
        for pt in series[1:]:
            if not pt.get("exit_time"):
                continue
            values.append(base + float(pt.get("cumulative") or 0))
        return values
    first_cum = 0.0
    for pt in series[1:]:
        if pt.get("exit_time"):
            first_cum = float(pt.get("cumulative") or 0)
            break
    lev = float(c.LEVERAGE or 5)
    margin = float(c.MARGIN_SIZE or 2)
    notional = float(c.NOTIONAL_SIZE or 10)
    anchor = max(abs(first_cum), notional, margin * (lev if lev > 0 else 5), 100.0)
    values: list[float] = []
    for pt in series[1:]:
        if not pt.get("exit_time"):
            continue
        values.append(anchor + float(pt.get("cumulative") or 0))
    return values


def _max_drawdown_from_close_series(series: list[dict], *, tab: str | None) -> dict | None:
    values = _dashboard_equity_values_from_series(series, tab=tab)
    return _max_drawdown_from_equity_values(values)


def _dashboard_equity_curve_api(
    *,
    tab: str | None = None,
    days: int = 0,
    max_points: int | None = None,
):
    """Full equity curve for GET /api/equity-curve (days=0 = all closes in state/cache)."""
    c = _core()
    rows = _filter_history_rows(_history_source_rows(), days=days)
    rows = _filter_history_rows_for_equity_curve(rows, tab)
    series = _equity_close_series_from_rows(rows)
    total_closes = max(0, len(series) - 1)
    max_drawdown = _max_drawdown_from_close_series(series, tab=tab)
    point_cap = int(max_points if max_points is not None else c.DASHBOARD_EQUITY_CURVE_MAX_POINTS or 0)
    downsampled = False
    if point_cap > 0 and len(series) > point_cap:
        series = _downsample_equity_series(series, point_cap)
        downsampled = True
    return {
        "series": series,
        "total_closes": total_closes,
        "max_drawdown": max_drawdown,
        "days": int(days),
        "tab": tab or "All",
        "downsampled": downsampled,
        "point_count": len(series),
        "source": _dashboard_recent_history_source(),
    }


def _empty_dashboard_strategy_stats_row():
    return {
        "trades": 0,
        "wins": 0,
        "best": None,
        "worst": None,
        "grossWin": 0.0,
        "grossLoss": 0.0,
    }


def _dashboard_strategy_stats_from_rows(rows):
    c = _core()
    tabs = {
        tab: _empty_dashboard_strategy_stats_row()
        for tab in list(c.TABS) + ["SafeGuard", "Recovered"]
    }
    side_pnl = {"Long": 0.0, "Short": 0.0}
    for entry in rows:
        tab = entry.get("tab") or "Recovered"
        if tab not in tabs:
            tabs[tab] = _empty_dashboard_strategy_stats_row()
        row = tabs[tab]
        pnl = float(entry.get("pnl_usd") or 0)
        row["trades"] = int(row.get("trades", 0) or 0) + 1
        if c._is_winning_trade(pnl):
            row["wins"] = int(row.get("wins", 0) or 0) + 1
        if pnl > 0:
            row["grossWin"] = float(row.get("grossWin", 0) or 0) + pnl
        elif pnl < 0:
            row["grossLoss"] = float(row.get("grossLoss", 0) or 0) + abs(pnl)
        best = row.get("best")
        worst = row.get("worst")
        if best is None or pnl > float(best):
            row["best"] = pnl
        if worst is None or pnl < float(worst):
            row["worst"] = pnl
        side = entry.get("side")
        if side in side_pnl:
            side_pnl[side] += pnl
    return tabs, side_pnl


def _dashboard_strategy_stats_api(*, days: int = 0):
    """Per-tab stats from full close history (same filter as GET /api/history)."""
    rows = _filter_history_rows(_history_source_rows(), days=days)
    tab_stats, side_pnl = _dashboard_strategy_stats_from_rows(rows)
    return {
        "tab_stats": tab_stats,
        "side_pnl": side_pnl,
        "total_closes": len(rows),
        "days": int(days),
        "source": _dashboard_recent_history_source(),
    }


def _recovered_tab_summary():
    c = _core()
    """LIVE: income attributed to Recovered (manual / unmatched closes)."""
    row = (c.state.get("binance_tab_income") or {}).get("Recovered") or {}
    gp = float(row.get("gross_profit", 0) or 0)
    gl = float(row.get("gross_loss", 0) or 0)
    stats = (c.state.get("tab_stats") or {}).get("Recovered") or {}
    return {
        "gross_profit": gp,
        "gross_loss": gl,
        "net": gp - gl,
        "trades": int(stats.get("trades", 0) or 0),
        "wins": int(stats.get("wins", 0) or 0),
    }


def _dashboard_stats_meta():
    c = _core()
    use_binance_closes = c._use_binance_close_cache()
    return {
        "trade_counts": "binance_closes" if use_binance_closes else (
            "tab_stats" if c.LIVE_MODE else "bot_history"
        ),
        "trade_pnl_per_close": "binance_user_trades" if use_binance_closes else "bot_history_reconciled",
        "live_realized_dollars": "binance_tab_income" if c.LIVE_MODE else "bot_history",
        "recent_trades_table": (
            "binance_user_trades" if use_binance_closes else (
                "bot_history" if c.LIVE_MODE else "bot_history"
            )
        ),
        "close_history_enabled": c.BINANCE_CLOSE_HISTORY_ENABLED,
        "balances_label": "strategy_ledger",
        "close_history_days": c.BINANCE_CLOSE_HISTORY_DAYS,
        "close_history_symbol_cap": c.BINANCE_CLOSE_HISTORY_SYMBOL_CAP,
        "close_history_ttl_sec": c.BINANCE_CLOSE_HISTORY_TTL_SEC,
        "close_history_refresh_symbol_cap": c.BINANCE_CLOSE_HISTORY_REFRESH_SYMBOL_CAP,
        "close_history_batch_size": c.BINANCE_CLOSE_HISTORY_BATCH_SIZE,
        "uds_account_fresh_sec": c.UDS_ACCOUNT_FRESH_SEC,
        "win_rule": "pnl_strictly_positive",
        "recovered": _recovered_tab_summary() if c.LIVE_MODE else None,
        "equity_curve_baseline": c._dashboard_equity_curve_baseline() if c.LIVE_MODE else None,
    }
