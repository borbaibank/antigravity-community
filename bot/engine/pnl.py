"""Unrealized PnL, income sync, and dashboard PnL helpers (extracted from bot.core)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import binance_live
from config import (
    EQUITY_CURVE_MARGIN_BASELINE,
    INITIAL_BALANCE,
    LOW_MARGIN_THRESHOLD,
    PIONEX_CONFIGURED,
)

def _core():
    from bot import core
    return core


def _position_mark_price(pos: dict):
    c = _core()
    """Best-effort live mark price for local testnet protection."""
    try:
        sym = pos.get("symbol")
        pos_side = c._position_side_from_state(pos)
        for live_pos in c.exchange_account.get("positions") or []:
            if live_pos.get("symbol") != sym:
                continue
            if str(live_pos.get("positionSide") or "").upper() != pos_side:
                continue
            mark = float(live_pos.get("markPrice") or 0)
            return mark if mark > 0 else None
    except Exception:
        return None
    return None


def _position_price(pos: dict):
    c = _core()
    """SL/TP trigger price (last or mark per SLTP_TRIGGER_PRICE)."""
    sym = pos.get("symbol")
    px = c._price_for_sltp(sym)
    if px is not None:
        return px
    return c._position_mark_price(pos)


def _unrealized_pnl_usd(entry: float, qty: float, mark: float, side: str):
    c = _core()
    """Futures unrealized PnL from entry, abs qty, and mark."""
    if entry <= 0 or qty <= 0 or mark <= 0:
        return 0.0
    if side == "Long":
        return (mark - entry) * qty
    return (entry - mark) * qty


def _refresh_exchange_account_marks():
    c = _core()
    """Refresh exchange snapshot marks + unrealized from WS mark feed (dashboard cadence)."""
    if not c.LIVE_MODE:
        return
    ea = c.exchange_account
    if not ea or ea.get("positions") is None:
        return
    positions = list(ea.get("positions") or [])
    if not positions:
        ea["unrealizedProfit"] = 0.0
        return
    total_ur = 0.0
    updated: list[dict] = []
    for p in positions:
        row = dict(p)
        sym = row.get("symbol")
        entry = float(row.get("entryPrice", 0) or 0)
        amt = abs(float(row.get("positionAmt", 0) or 0))
        side = row.get("side") or (
            "Long" if float(row.get("positionAmt", 0) or 0) > 0 else "Short"
        )
        mark = c._mark_or_last(sym)
        if mark is None or mark <= 0:
            mark = float(row.get("markPrice", 0) or 0)
        if mark > 0:
            row["markPrice"] = mark
            if entry > 0 and amt > 0:
                row["unrealizedProfit"] = _unrealized_pnl_usd(entry, amt, mark, side)
        total_ur += float(row.get("unrealizedProfit", 0) or 0)
        updated.append(row)
    ea["positions"] = updated
    ea["unrealizedProfit"] = total_ur
    wallet = float(ea.get("walletBalance", 0) or 0)
    if wallet > 0:
        ea["marginBalance"] = wallet + total_ur


def _recalculate_unrealized_pnls():
    c = _core()
    unrealized_pnls = {tab: 0.0 for tab in c.TABS}
    unrealized_pnls["SafeGuard"] = 0.0
    unrealized_pnls["Recovered"] = 0.0

    ea_by_leg: dict[tuple[str, str], dict] = {}
    grouped_bot_qty: dict[tuple[str, str], float] = {}
    if c.LIVE_MODE:
        for ep in c.exchange_account.get("positions") or []:
            sym = ep.get("symbol")
            side = ep.get("side")
            if sym and side:
                ea_by_leg[(sym, side)] = ep
        for pos in c.state.get("open_positions", {}).values():
            sym = pos.get("symbol")
            side = pos.get("side")
            if sym and side:
                leg = (sym, side)
                grouped_bot_qty[leg] = grouped_bot_qty.get(leg, 0.0) + float(pos.get("qty", 0) or 0)

    for pos in c.state.get("open_positions", {}).values():
        tab = pos.get("tab")
        if tab not in unrealized_pnls:
            unrealized_pnls[tab] = 0.0
        qty = float(pos.get("qty", 0) or 0)
        if qty <= 0:
            continue
        sym = pos.get("symbol")
        side = pos.get("side")
        if c.LIVE_MODE and sym and side:
            entry = float(pos.get("entry_price", 0) or 0)
            mark = c._mark_or_last(sym)
            if mark and mark > 0 and entry > 0:
                unrealized_pnls[tab] += _unrealized_pnl_usd(entry, qty, mark, side)
                continue
            leg = (sym, side)
            ea = ea_by_leg.get(leg)
            group_qty = grouped_bot_qty.get(leg, 0.0)
            if ea and group_qty > 0:
                ur = float(ea.get("unrealizedProfit", 0) or 0)
                unrealized_pnls[tab] += ur * (qty / group_qty)
                continue
        price = _position_price(pos)
        if price is None:
            continue
        entry = float(pos.get("entry_price", 0) or 0)
        if entry <= 0:
            continue
        if pos.get("side") == "Long":
            unrealized_pnls[tab] += (price - entry) * qty
        else:
            unrealized_pnls[tab] += (entry - price) * qty
    for tab, value in unrealized_pnls.items():
        c.state["unrealized_pnls"][tab] = value


def _binance_income_net():
    c = _core()
    inc = c.state.get("binance_income") or {}
    return (
        float(inc.get("realized_pnl", 0) or 0)
        + float(inc.get("commission", 0) or 0)
        + float(inc.get("funding", 0) or 0)
    )


def _strategy_realized_total():
    c = _core()
    """Sum per-tab Binance income (matches dashboard strategy realized)."""
    if not c.LIVE_MODE:
        return sum(float(e.get("pnl_usd", 0) or 0) for e in c.state.get("history", []))
    tab_inc = c.state.get("binance_tab_income") or {}
    total = 0.0
    for tab in list(c.TABS) + ["SafeGuard", "Recovered"]:
        gp, gl = _effective_tab_gross(tab, tab_inc)
        total += gp - gl
    return total


def _ensure_equity_curve_baselines():
    c = _core()
    """Freeze account/strategy realized at first run so the curve starts at 0."""
    inc = c.state.setdefault("binance_income", {})
    acct = _binance_income_net()
    strat = _strategy_realized_total()
    snaps = c.state.get("equity_snapshots") or []
    if "curve_baseline_account" not in inc:
        if snaps:
            inc["curve_baseline_account"] = float(
                snaps[0].get("account_realized", acct) or acct
            )
            inc["curve_baseline_strategy"] = float(
                snaps[0].get(
                    "strategy_realized",
                    snaps[0].get("account_realized", strat),
                )
                or strat
            )
        else:
            inc["curve_baseline_account"] = acct
            inc["curve_baseline_strategy"] = strat
    inc.setdefault(
        "curve_baseline_strategy",
        float(inc.get("curve_baseline_account", strat) or strat),
    )
    return (
        float(inc.get("curve_baseline_account", 0) or 0),
        float(inc.get("curve_baseline_strategy", 0) or 0),
    )


def _dashboard_equity_curve_baseline():
    c = _core()
    if not c.LIVE_MODE:
        return {}
    acct_base, strat_base = _ensure_equity_curve_baselines()
    return {"account": acct_base, "strategy": strat_base}


def _append_equity_snapshot(force: bool = False):
    c = _core()
    """Persist occasional account realized snapshots (survives HISTORY_CAP trim)."""
    if not c.LIVE_MODE:
        return
    _ensure_equity_curve_baselines()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    snaps = c.state.setdefault("equity_snapshots", [])
    if snaps and not force:
        last_ms = int(snaps[-1].get("ts_ms", 0) or 0)
        if now_ms - last_ms < 3_600_000:
            return
    snaps.append({
        "ts_ms": now_ms,
        "account_realized": _binance_income_net(),
        "strategy_realized": _strategy_realized_total(),
        "realized_pnl": float((c.state.get("binance_income") or {}).get("realized_pnl", 0) or 0),
        "commission": float((c.state.get("binance_income") or {}).get("commission", 0) or 0),
        "funding": float((c.state.get("binance_income") or {}).get("funding", 0) or 0),
    })
    if len(snaps) > 2500:
        c.state["equity_snapshots"] = snaps[-2500:]


_BINANCE_GROSS_TYPES = frozenset({"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"})
_GROSS_BREAKDOWN_VERSION = 2  # v2: include FUNDING_FEE + full income backfill
_INCOME_TAB_MATCH_MS = 300_000
_INCOME_SYNC_POLL_SEC = 120.0                   # idle poll when no recent close
_INCOME_SYNC_POLL_ACTIVE_SEC = 60.0            # poll while closes are recent
_INCOME_SYNC_IDLE_AFTER_CLOSE_SEC = 600.0       # 10m — then switch to idle poll
_INCOME_TODAY_REFRESH_SEC = 300.0               # Today Profit card refresh (5m)
_INCOME_DAILY_30D_REFRESH_SEC = 1800.0          # 30-day daily profit bar chart (30m)
_DAILY_PROFIT_30D_DAYS = 30
_SEEN_TRAN_IDS_CAP = 50_000


def _empty_tab_income():
    c = _core()
    return {"gross_profit": 0.0, "gross_loss": 0.0}


def _normalize_binance_income_state():
    c = _core()
    inc = c.state.setdefault("binance_income", {})
    inc.setdefault("realized_pnl", 0.0)
    inc.setdefault("commission", 0.0)
    inc.setdefault("funding", 0.0)
    inc.setdefault("last_ts", 0)
    if "seed_ts" not in inc:
        inc["seed_ts"] = int(inc.get("last_ts", 0) or 0)
    inc.setdefault("gross_profit", 0.0)
    inc.setdefault("gross_loss", 0.0)
    inc.setdefault("seen_tran_ids", [])
    inc.setdefault("gross_rebuilt", False)
    inc.setdefault("gross_seed_ts", 0)
    inc.setdefault("income_tran_high_water", 0)
    if int(inc.get("gross_breakdown_version", 0) or 0) < _GROSS_BREAKDOWN_VERSION:
        inc["gross_breakdown_version"] = _GROSS_BREAKDOWN_VERSION
        inc["gross_rebuilt"] = False
        inc["gross_seed_ts"] = 0
    return inc


def _normalize_binance_tab_income():
    c = _core()
    tabs = c.state.setdefault("binance_tab_income", {})
    for tab in list(c.TABS) + ["SafeGuard", "Recovered"]:
        if tab not in tabs or not isinstance(tabs.get(tab), dict):
            tabs[tab] = _empty_tab_income()
        else:
            entry = tabs[tab]
            entry.setdefault("gross_profit", 0.0)
            entry.setdefault("gross_loss", 0.0)
    return tabs


def _income_seen_tran_set(inc: dict):
    c = _core()
    seen: set[int] = set()
    for raw in inc.get("seen_tran_ids") or []:
        try:
            tid = int(raw)
        except (TypeError, ValueError):
            continue
        if tid > 0:
            seen.add(tid)
    return seen


def _income_tran_already_applied(inc: dict, tran_id: int, seen: set[int]):
    c = _core()
    """Dedup income rows even after seen_tran_ids list is pruned."""
    if tran_id <= 0:
        return False
    if tran_id in seen:
        return True
    return tran_id <= int(inc.get("income_tran_high_water", 0) or 0)


def _mark_income_tran_applied(inc: dict, tran_id: int, seen: set[int]):
    c = _core()
    if tran_id > 0:
        seen.add(tran_id)
        inc["income_tran_high_water"] = max(
            int(inc.get("income_tran_high_water", 0) or 0),
            tran_id,
        )


def _persist_income_seen_tran(inc: dict, seen: set[int]):
    c = _core()
    if not seen:
        inc["seen_tran_ids"] = []
        return
    ordered = sorted(seen)
    if len(ordered) > _SEEN_TRAN_IDS_CAP:
        ordered = ordered[-_SEEN_TRAN_IDS_CAP:]
    inc["seen_tran_ids"] = ordered
    inc["income_tran_high_water"] = max(
        int(inc.get("income_tran_high_water", 0) or 0),
        ordered[-1] if ordered else 0,
    )


def _accumulate_gross_breakdown(bucket: dict, value: float):
    c = _core()
    v = float(value or 0)
    if v >= 0:
        bucket["gross_profit"] = float(bucket.get("gross_profit", 0) or 0) + v
    else:
        bucket["gross_loss"] = float(bucket.get("gross_loss", 0) or 0) + abs(v)


def _binance_gross_net(gross_profit: float, gross_loss: float):
    c = _core()
    return float(gross_profit or 0) - float(gross_loss or 0)


def _tab_gross_from_history(tab: str):
    c = _core()
    """Sum closed-trade gross win/loss for one tab from bot history."""
    gross_profit = 0.0
    gross_loss = 0.0
    for entry in c.state.get("history", []):
        if (entry.get("tab") or "Recovered") != tab:
            continue
        pnl = float(entry.get("pnl_usd") or 0)
        if pnl >= 0:
            gross_profit += pnl
        else:
            gross_loss += abs(pnl)
    return gross_profit, gross_loss


def _effective_tab_gross(tab: str, tab_inc: dict):
    c = _core()
    """Per-tab gross for dashboard — Binance income unless attribution clearly incomplete."""
    bi = tab_inc.get(tab) or {}
    gp = float(bi.get("gross_profit", 0) or 0)
    gl = float(bi.get("gross_loss", 0) or 0)
    hist_gp, hist_gl = _tab_gross_from_history(tab)
    hist_total = hist_gp + hist_gl
    bin_total = gp + gl
    if tab not in ("SafeGuard", "Recovered") and hist_total > 0.000001 and bin_total < hist_total * 0.5:
        return hist_gp, hist_gl
    return gp, gl


def _history_entry_position_side(entry: dict):
    c = _core()
    ps = str(entry.get("position_side") or "").upper()
    if ps in {"LONG", "SHORT"}:
        return ps
    return c._position_side_name(entry.get("side")).upper()


def _attribute_income_to_tab(
    symbol: str,
    ts_ms: int,
    *,
    position_side: str | None = None,
):
    c = _core()
    """Map one /fapi/v1/income row to a strategy tab (hedge-safe when possible)."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return "Recovered"
    pos_side = str(position_side or "").strip().upper()

    def _side_matches(entry: dict) -> bool:
        if not pos_side:
            return True
        entry_ps = str(entry.get("position_side") or "").upper()
        if not entry_ps:
            entry_ps = _history_entry_position_side(entry)
        return not entry_ps or entry_ps == pos_side

    best_tab = None
    best_delta = None

    for entry in c._position_registry().values():
        if entry.get("status") != "closed":
            continue
        if str(entry.get("symbol") or "").upper() != sym:
            continue
        if not _side_matches(entry):
            continue
        closed_ms = c._dt_to_ms(entry.get("closed_at"))
        if closed_ms is None:
            continue
        delta = abs(int(ts_ms) - closed_ms)
        if delta > _INCOME_TAB_MATCH_MS:
            continue
        tab = entry.get("tab")
        if not tab:
            continue
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best_tab = tab
    if best_tab:
        return best_tab

    for entry in reversed(c.state.get("history", [])):
        if str(entry.get("symbol") or "").upper() != sym:
            continue
        if not _side_matches(entry):
            continue
        close_oid = entry.get("close_order_id")
        exit_ms = c._dt_to_ms(entry.get("exit_time"))
        if exit_ms is None:
            continue
        delta = abs(int(ts_ms) - exit_ms)
        if delta > _INCOME_TAB_MATCH_MS:
            continue
        tab = entry.get("tab")
        if not tab:
            continue
        if close_oid:
            delta = min(delta, 5_000)
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best_tab = tab
    return best_tab or "Recovered"


def _apply_income_gross_record(
    record: dict,
    *,
    inc: dict,
    tabs: dict,
    seen: set[int],
):
    c = _core()
    """Gross profit/loss + per-tab attribution only (no cumulative income totals)."""
    tran_id = int(record.get("tranId") or 0)
    if tran_id > 0 and tran_id in seen:
        return False
    if tran_id > 0:
        seen.add(tran_id)

    t = str(record.get("incomeType") or "")
    if t not in _BINANCE_GROSS_TYPES:
        return tran_id > 0

    v = float(record.get("income") or 0)
    ts = int(record.get("time") or 0)
    sym = str(record.get("symbol") or "")
    _accumulate_gross_breakdown(inc, v)
    tab = _attribute_income_to_tab(sym, ts)
    if tab not in tabs:
        tabs[tab] = _empty_tab_income()
    _accumulate_gross_breakdown(tabs[tab], v)
    return True


def _apply_income_record(
    record: dict,
    *,
    inc: dict,
    tabs: dict,
    seen: set[int],
):
    c = _core()
    """Apply one /fapi/v1/income row; skip duplicates via tranId."""
    tran_id = int(record.get("tranId") or 0)
    if _income_tran_already_applied(inc, tran_id, seen):
        return False
    _mark_income_tran_applied(inc, tran_id, seen)

    t = str(record.get("incomeType") or "")
    v = float(record.get("income") or 0)
    ts = int(record.get("time") or 0)
    sym = str(record.get("symbol") or "")

    if t == "REALIZED_PNL":
        inc["realized_pnl"] = float(inc.get("realized_pnl", 0) or 0) + v
    elif t == "COMMISSION":
        inc["commission"] = float(inc.get("commission", 0) or 0) + v
    elif t == "FUNDING_FEE":
        inc["funding"] = float(inc.get("funding", 0) or 0) + v
    else:
        return tran_id > 0

    if ts > int(inc.get("last_ts", 0) or 0):
        inc["last_ts"] = ts

    if t in _BINANCE_GROSS_TYPES:
        _accumulate_gross_breakdown(inc, v)
        tab = _attribute_income_to_tab(sym, ts)
        if tab not in tabs:
            tabs[tab] = _empty_tab_income()
        _accumulate_gross_breakdown(tabs[tab], v)
    return True


def _utc_day_start_ms(now: datetime | None = None):
    c = _core()
    now = now or datetime.now(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(day.timestamp() * 1000)


def _utc_day_str_from_ms(ts_ms: int):
    c = _core()
    return datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _daily_profit_30d_date_keys(now: datetime | None = None):
    c = _core()
    """UTC calendar dates for the last 30 days inclusive (oldest first)."""
    now = now or datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return [
        (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(_DAILY_PROFIT_30D_DAYS - 1, -1, -1)
    ]


def _group_income_by_utc_day(records: list):
    c = _core()
    """Bucket income rows by UTC date; each bucket uses Today Profit formula."""
    buckets: dict[str, list] = {}
    for r in records or []:
        ts = int(r.get("time") or 0)
        if ts <= 0:
            continue
        day = _utc_day_str_from_ms(ts)
        buckets.setdefault(day, []).append(r)
    return {day: _summarize_income_records(rows) for day, rows in buckets.items()}


def _build_daily_profit_30d_series(by_day: dict[str, dict], now: datetime | None = None):
    c = _core()
    series: list[dict] = []
    for date_utc in _daily_profit_30d_date_keys(now):
        totals = by_day.get(date_utc) or {}
        series.append({
            "date_utc": date_utc,
            "net": float(totals.get("net", 0) or 0),
            "realized_pnl": float(totals.get("realized_pnl", 0) or 0),
            "commission": float(totals.get("commission", 0) or 0),
            "funding": float(totals.get("funding", 0) or 0),
        })
    return series


def _summarize_income_records(records: list):
    c = _core()
    """Sum REALIZED_PNL + COMMISSION + FUNDING_FEE (matches Binance Today PnL)."""
    totals = {"realized_pnl": 0.0, "commission": 0.0, "funding": 0.0}
    for r in records or []:
        t = str(r.get("incomeType") or "")
        v = float(r.get("income") or 0)
        if t == "REALIZED_PNL":
            totals["realized_pnl"] += v
        elif t == "COMMISSION":
            totals["commission"] += v
        elif t == "FUNDING_FEE":
            totals["funding"] += v
    totals["net"] = totals["realized_pnl"] + totals["commission"] + totals["funding"]
    return totals


async def _sync_today_income_once():
    c = _core()
    """Fetch today's Binance income (UTC day) for dashboard Today Profit card."""
    if not c.LIVE_MODE or c._http_client is None or c._binance_rate_limited():
        return
    import time as _time_mod
    now = datetime.now(timezone.utc)
    date_utc = now.strftime("%Y-%m-%d")
    start_ms = _utc_day_start_ms(now)
    end_ms = int(now.timestamp() * 1000)
    all_records: list = []
    cursor = start_ms
    while True:
        records = await binance_live.get_income(
            c._http_client, start_time=cursor, end_time=end_ms, limit=1000,
        )
        if not records:
            break
        all_records.extend(records)
        if len(records) < 1000:
            break
        cursor = int(records[-1].get("time") or cursor) + 1
    totals = _summarize_income_records(all_records)
    totals["date_utc"] = date_utc
    c._today_binance_profit = totals
    c._last_income_today_sync_mono = _time_mod.monotonic()


async def _sync_daily_profit_30d_once():
    c = _core()
    """Fetch last 30 UTC days of Binance income for dashboard daily profit bars."""
    if not c.LIVE_MODE or c._http_client is None or c._binance_rate_limited():
        return
    import time as _time_mod
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_ms = int((today_start - timedelta(days=_DAILY_PROFIT_30D_DAYS - 1)).timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)
    all_records: list = []
    cursor = start_ms
    while True:
        records = await binance_live.get_income(
            c._http_client, start_time=cursor, end_time=end_ms, limit=1000,
        )
        if not records:
            break
        all_records.extend(records)
        if len(records) < 1000:
            break
        cursor = int(records[-1].get("time") or cursor) + 1
        if cursor >= end_ms:
            break
    by_day = _group_income_by_utc_day(all_records)
    c._daily_profit_30d = _build_daily_profit_30d_series(by_day, now)
    c._last_daily_profit_30d_sync_mono = _time_mod.monotonic()


async def _maybe_sync_daily_profit_30d():
    c = _core()
    import time as _time_mod
    if not c.LIVE_MODE or c._http_client is None or c._binance_rate_limited():
        return
    now_mono = _time_mod.monotonic()
    if (
        c._daily_profit_30d
        and now_mono - c._last_daily_profit_30d_sync_mono < _INCOME_DAILY_30D_REFRESH_SEC
    ):
        return
    try:
        await _sync_daily_profit_30d_once()
    except Exception as e:
        print(f"[Income Sync] daily profit 30d refresh error: {e}")


async def _bootstrap_daily_profit_30d():
    c = _core()
    await asyncio.sleep(8)
    try:
        await _sync_daily_profit_30d_once()
    except Exception as e:
        print(f"[Income Sync] daily profit 30d bootstrap error: {e}")


def _dashboard_daily_profit_30d():
    c = _core()
    if not c.LIVE_MODE or not c._daily_profit_30d:
        return None
    return [dict(row) for row in c._daily_profit_30d]


def _dashboard_today_profit():
    c = _core()
    if not c.LIVE_MODE:
        return None
    tp = c._today_binance_profit or {}
    if not tp.get("date_utc"):
        return None
    return {
        "net": float(tp.get("net", 0) or 0),
        "realized_pnl": float(tp.get("realized_pnl", 0) or 0),
        "commission": float(tp.get("commission", 0) or 0),
        "funding": float(tp.get("funding", 0) or 0),
        "date_utc": tp.get("date_utc"),
    }


def _exchange_unrealized_total():
    c = _core()
    total = 0.0
    for pos in c.exchange_account.get("positions", []) or []:
        try:
            amt = abs(float(pos.get("positionAmt", 0) or 0))
            if amt <= 0:
                continue
            total += float(pos.get("unRealizedProfit", pos.get("unrealizedProfit", 0)) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _dashboard_exchange_account():
    c = _core()
    """Return exchange snapshot for dashboard only when populated."""
    if not c.LIVE_MODE:
        return None
    ea = c.exchange_account or {}
    if ea.get("walletBalance") is None or ea.get("positions") is None:
        return None
    out = dict(ea)
    today = _dashboard_today_profit()
    if today is not None:
        out["today_profit"] = today
    return out


def _dashboard_pionex_balance():
    c = _core()
    """Return cached Pionex wallet snapshot for dashboard (no secrets)."""
    return dict(c._pionex_balance_snapshot)


def _dashboard_pnl_summary():
    c = _core()
    """Return strategy + account PnL for dashboard cards.

    ``strategy`` / ``all`` — bot history + per-tab unrealized (matches Strategy table).
    ``account`` (live only) — Binance income net + exchange position unrealized.
    """
    per_tab = {
        tab: {"realized": 0.0, "unrealized": 0.0, "total": 0.0}
        for tab in list(c.TABS) + ["SafeGuard", "Recovered"]
    }
    for entry in c.state.get("history", []):
        tab = entry.get("tab")
        if tab not in per_tab:
            per_tab[tab] = {"realized": 0.0, "unrealized": 0.0, "total": 0.0}
        per_tab[tab]["realized"] += float(entry.get("pnl_usd", 0) or 0)

    for tab, value in (c.state.get("unrealized_pnls") or {}).items():
        if tab not in per_tab:
            per_tab[tab] = {"realized": 0.0, "unrealized": 0.0, "total": 0.0}
        per_tab[tab]["unrealized"] = float(value or 0)

    if c.LIVE_MODE:
        tab_inc = c.state.get("binance_tab_income") or {}
        for tab, item in per_tab.items():
            gp, gl = _effective_tab_gross(tab, tab_inc)
            item["gross_profit"] = gp
            item["gross_loss"] = gl
            item["realized"] = gp - gl
            item["total"] = item["realized"] + float(item.get("unrealized", 0) or 0)

    for item in per_tab.values():
        if "total" not in item or not c.LIVE_MODE:
            item["total"] = item["realized"] + item["unrealized"]

    strat_realized = sum(item["realized"] for item in per_tab.values())
    if c.LIVE_MODE:
        strat_unrealized = sum(
            float(item["unrealized"])
            for tab_name, item in per_tab.items()
            if tab_name != "Recovered"
        )
    else:
        strat_unrealized = sum(item["unrealized"] for item in per_tab.values())

    strategy = {
        "realized": strat_realized,
        "unrealized": strat_unrealized,
        "total": strat_realized + strat_unrealized,
    }

    account = None
    if c.LIVE_MODE:
        inc = c.state.get("binance_income") or {}
        acct_realized = _binance_income_net()
        acct_unrealized = _exchange_unrealized_total()
        gp = float(inc.get("gross_profit", 0) or 0)
        gl = float(inc.get("gross_loss", 0) or 0)
        gross_net = _binance_gross_net(gp, gl)
        account = {
            "realized": acct_realized,
            "unrealized": acct_unrealized,
            "total": acct_realized + acct_unrealized,
            "realized_pnl": float(inc.get("realized_pnl", 0) or 0),
            "commission": float(inc.get("commission", 0) or 0),
            "funding": float(inc.get("funding", 0) or 0),
            "gross_profit": gp,
            "gross_loss": gl,
            "gross_net": gross_net,
            "today": _dashboard_today_profit(),
        }

    return {
        "all": strategy,
        "strategy": strategy,
        "account": account,
        "per_tab": per_tab,
        "source": "strategy_and_account" if c.LIVE_MODE else "bot_history_plus_open_state",
    }
