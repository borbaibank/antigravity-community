"""Price feed, scan universe, and mark price helpers."""

from __future__ import annotations

import asyncio
import time

import binance_live
from config import (
    KLINE_FETCH_DELAY_SEC,
    KLINE_FETCH_MIN_DELAY_SEC,
    MAX_FUNDING_RATE_ABS,
    MAX_SPREAD_PCT,
    PRICE_FEED_BASE_URL,
    PRICE_FEED_ENV,
    SLIPPAGE_PCT,
)

def _core():
    from bot import core
    return core


_EXCLUDE_SYMBOLS = {
    "USDCUSDT","FDUSDUSDT","TUSDUSDT","BUSDUSDT","USDDUSDT",
    "USDPUSDT","USTUSDT","FRAXUSDT","DAIUSDT","EURUSDT",
    # Tokenized gold (COIN/RWA — not TradFi COMMODITY)
    "XAUTUSDT","PAXGUSDT",
    # TradFi commodities (also caught by COMMODITY underlyingType — listed for explicit intent)
    "NATGASUSDT","COPPERUSDT",
}
# TradFi scan: keep stocks/ETFs; drop commodities + forex/currency by exchange metadata.
_SCAN_EXCLUDE_UNDERLYING_TYPES = frozenset({"COMMODITY", "FOREX", "CURRENCY", "FX"})


def _scan_exclude_symbols(exchange_info: dict):
    c = _core()
    excluded = set(_EXCLUDE_SYMBOLS)
    for s in exchange_info.get("symbols", []):
        sym = s.get("symbol")
        if not sym or not sym.endswith("USDT"):
            continue
        if s.get("underlyingType") in _SCAN_EXCLUDE_UNDERLYING_TYPES:
            excluded.add(sym)
    return excluded

async def _price_feed_get(path: str, params: dict | None = None):
    c = _core()
    r = await c._http_client.get(c.PRICE_FEED_BASE_URL + path, params=params or {})
    r.raise_for_status()
    return r.json()


async def fetch_scan_symbols():
    c = _core()
    """Load top symbols by 24h quoteVolume from the configured Binance Futures price feed."""
    try:
        tickers, exchange_info = await asyncio.gather(
            c._price_feed_get("/fapi/v1/ticker/24hr"),
            c._price_feed_get("/fapi/v1/exchangeInfo"),
        )
        tradfi_symbols = {
            s.get("symbol")
            for s in exchange_info.get("symbols", [])
            if s.get("contractType") == "TRADIFI_PERPETUAL"
            or "TradFi" in (s.get("underlyingSubType") or [])
        }
        exclude_symbols = c._scan_exclude_symbols(exchange_info)
        filtered = [
            t for t in tickers
            if t["symbol"].endswith("USDT")
            and t["symbol"] not in exclude_symbols
            and t["symbol"].isascii()
            and t["symbol"].replace("USDT", "").isalnum()
        ]
        filtered.sort(key=lambda t: float(t.get("quoteVolume", 0)), reverse=True)
        scan_limit = c._scan_universe_size()
        c.SCAN_SYMBOLS = [t["symbol"] for t in filtered[:scan_limit]]
        c.SCAN_TICKER_BY_SYM = {t["symbol"]: t for t in filtered[:scan_limit]}
        tradfi_in_scan = sum(1 for s in c.SCAN_SYMBOLS if s in tradfi_symbols)
        print(f"Top {scan_limit} loaded ({len(c.SCAN_SYMBOLS)}) from Binance {c.PRICE_FEED_ENV} price feed. "
              f"#1={c.SCAN_SYMBOLS[0] if c.SCAN_SYMBOLS else '-'}  "
              f"#{scan_limit}={c.SCAN_SYMBOLS[-1] if len(c.SCAN_SYMBOLS) >= scan_limit else '-'} "
              f"| TradFi in scan={tradfi_in_scan}/{len(tradfi_symbols)}")
    except Exception as e:
        print(f"[WARN] fetch_scan_symbols failed: {e} - c.SCAN_SYMBOLS unchanged ({len(c.SCAN_SYMBOLS)} symbols)")

async def _check_entry_quality(sym: str, side: str):
    c = _core()
    """Return (ok, reason). Skips entry when funding rate is extreme or spread is wide.

    Funding rule: reject longs when funding is very positive (longs pay shorts),
    reject shorts when very negative. Absolute cap also applies either way.
    Spread rule: reject when (ask - bid) / mid exceeds c.MAX_SPREAD_PCT.
    """
    try:
        funding_data = await c._price_feed_get("/fapi/v1/premiumIndex", {"symbol": sym})
        funding = float(funding_data.get("lastFundingRate", 0) or 0)
    except Exception as e:
        print(f"[Filter] funding fetch failed {sym}: {e} — allowing entry")
        funding = 0.0
    if abs(funding) > c.MAX_FUNDING_RATE_ABS:
        if (side == "Long" and funding > 0) or (side == "Short" and funding < 0):
            return False, f"funding {funding*100:.3f}% hostile to {side}"

    try:
        book = await c._price_feed_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
        bid = float(book.get("bidPrice", 0) or 0)
        ask = float(book.get("askPrice", 0) or 0)
    except Exception as e:
        print(f"[Filter] bookTicker fetch failed {sym}: {e} — allowing entry")
        return True, ""
    if bid <= 0 or ask <= 0:
        return True, ""
    mid = (bid + ask) / 2
    spread_pct = (ask - bid) / mid if mid else 0.0
    if spread_pct > c.MAX_SPREAD_PCT:
        return False, f"spread {spread_pct*100:.3f}% > {c.MAX_SPREAD_PCT*100:.3f}%"
    return True, ""


def _effective_kline_fetch_delay_sec():
    c = _core()
    return max(int(c.KLINE_FETCH_DELAY_SEC), int(c.KLINE_FETCH_MIN_DELAY_SEC))


def _mark_within_entry_protection(
    sym: str,
    side: str,
    mark: float,
    sl: float,
    tp: float,
):
    c = _core()
    """True when mark sits between planned SL and TP (same rule as paper entry guard)."""
    if mark <= 0 or sl <= 0 or tp <= 0:
        return False, f"invalid mark/SL/TP (mark={mark}, SL={sl}, TP={tp})"
    side_s = str(side or "")
    if side_s == "Long":
        if not (sl < mark < tp):
            return False, (
                f"mark {c._log_price(sym, mark)} outside Long protection "
                f"(SL={c._log_price(sym, sl)}, TP={c._log_price(sym, tp)})"
            )
    elif side_s == "Short":
        if not (tp < mark < sl):
            return False, (
                f"mark {c._log_price(sym, mark)} outside Short protection "
                f"(SL={c._log_price(sym, sl)}, TP={c._log_price(sym, tp)})"
            )
    else:
        return False, f"unknown side {side!r}"
    return True, ""


async def _pre_entry_mark_protection_guard(sym: str, sig: dict):
    c = _core()
    trigger_px = await c._fetch_sltp_trigger_price(sym)
    if not trigger_px or trigger_px <= 0:
        trigger_px = c._price_for_sltp(sym)
    if not trigger_px or trigger_px <= 0:
        return False, "no trigger price for entry protection check"
    ok, reason = c._mark_within_entry_protection(
        sym,
        sig.get("side") or "",
        float(trigger_px),
        float(sig["sl"]),
        float(sig["tp"]),
    )
    if not ok:
        return False, reason
    return True, ""


async def _resolve_live_entry_fill(
    sym: str,
    entry_res: dict,
    *,
    entry_client_id: str | None = None,
):
    c = _core()
    """Resolve avg fill price/qty from order response, order query, or userTrades — never guess from ep."""
    order_id = int(entry_res.get("orderId") or 0)
    avg_px = float(entry_res.get("avgPrice") or 0.0)
    ex_qty = float(entry_res.get("executedQty") or 0.0)
    if avg_px > 0 and ex_qty > 0:
        return avg_px, ex_qty, "order"
    if not c._http_client:
        return 0.0, 0.0, ""

    for delay in (0.2, 0.3, 0.5, 1.0):
        await asyncio.sleep(delay)
        try:
            order = await binance_live.get_order(
                c._http_client,
                sym,
                order_id=order_id if order_id > 0 else None,
                orig_client_order_id=entry_client_id if order_id <= 0 else None,
            )
        except Exception as e:
            print(f"[Live] get_order fill poll failed {sym}: {e}")
            continue
        avg_px = float(order.get("avgPrice") or 0.0)
        ex_qty = float(order.get("executedQty") or 0.0)
        if avg_px > 0 and ex_qty > 0:
            return avg_px, ex_qty, "get_order"

    import time
    now_ms = int(time.time() * 1000)
    try:
        trades = await binance_live.get_account_trades(
            c._http_client,
            sym,
            start_time=now_ms - 120_000,
            limit=100,
        )
    except Exception as e:
        print(f"[Live] userTrades fill lookup failed {sym}: {e}")
        return 0.0, 0.0, ""

    matched = []
    for trade in trades or []:
        if order_id > 0 and int(trade.get("orderId") or 0) == order_id:
            matched.append(trade)
        elif entry_client_id and str(trade.get("clientOrderId") or "") == entry_client_id:
            matched.append(trade)
    if not matched:
        return 0.0, 0.0, ""
    qty_sum = sum(float(t.get("qty") or 0) for t in matched)
    quote_sum = sum(float(t.get("price") or 0) * float(t.get("qty") or 0) for t in matched)
    if qty_sum <= 0 or quote_sum <= 0:
        return 0.0, 0.0, ""
    return quote_sum / qty_sum, qty_sum, "userTrades"


def _position_in_local_monitor_grace(pos: dict):
    c = _core()
    if not c._position_needs_local_exit_monitor(pos):
        return False
    import time as _time_mod
    until = float(pos.get("local_monitor_after_mono") or 0)
    return until > 0 and _time_mod.monotonic() < until


async def _fetch_mark_price(sym: str):
    c = _core()
    """Binance mark price (premiumIndex) — used for paper fills/exits and live preflight."""
    try:
        data = await c._price_feed_get("/fapi/v1/premiumIndex", {"symbol": sym})
        mark = float(data.get("markPrice") or 0)
        if mark > 0:
            c.latest_marks[sym] = mark
            return mark
    except Exception as e:
        print(f"[Mark] premiumIndex fetch failed {sym}: {e}")
    cached = c.latest_marks.get(sym)
    if cached and cached > 0:
        return cached
    last = c.latest_prices.get(sym)
    return last if last and last > 0 else None


async def _refresh_dashboard_market_prices():
    c = _core()
    """Bootstrap Live Prices cards (BTC/ETH/XAU/CL) — TradFi marks may lag behind WS startup."""
    for sym in sorted(c._DASHBOARD_MARKET_SYMBOLS):
        if c._mark_or_last(sym):
            continue
        await c._fetch_mark_price(sym)


def _mark_or_last(sym: str | None):
    c = _core()
    """Best-effort mark (futures index) then last trade price."""
    if not sym:
        return None
    m = c.latest_marks.get(sym)
    if m is not None and m > 0:
        return m
    p = c.latest_prices.get(sym)
    return p if p is not None and p > 0 else None


def _last_or_mark(sym: str | None):
    c = _core()
    """Best-effort last trade price then mark (fallback)."""
    if not sym:
        return None
    p = c.latest_prices.get(sym)
    if p is not None and p > 0:
        return p
    m = c.latest_marks.get(sym)
    return m if m is not None and m > 0 else None


def _entry_reference_price(sym: str | None):
    c = _core()
    """Price for entry wait / -4192 retry — last or mark per ENTRY_TRIGGER_PRICE."""
    if not sym:
        return None
    if c.ENTRY_TRIGGER_PRICE == "mark":
        return c._mark_or_last(sym)
    return c._last_or_mark(sym)


async def _fetch_entry_reference_price(sym: str):
    c = _core()
    """REST/WS refresh for entry wait price (last or mark)."""
    if c.ENTRY_TRIGGER_PRICE == "mark":
        cached = c._mark_or_last(sym)
        if cached and cached > 0:
            return cached
        return await c._fetch_mark_price(sym)
    cached = c._last_or_mark(sym)
    if cached and cached > 0:
        return cached
    try:
        data = await c._price_feed_get("/fapi/v1/ticker/price", {"symbol": sym})
        last = float(data.get("price") or 0)
        if last > 0:
            c.latest_prices[sym] = last
            return last
    except Exception as e:
        print(f"[Price] last fetch failed {sym}: {e}")
    return await c._fetch_mark_price(sym)


def _price_for_sltp(sym: str | None):
    c = _core()
    """Price feed for SL/TP triggers — last or mark per SLTP_TRIGGER_PRICE."""
    if not sym:
        return None
    if c.SLTP_TRIGGER_PRICE == "mark":
        return c._mark_or_last(sym)
    return c._last_or_mark(sym)


async def _fetch_sltp_trigger_price(sym: str):
    c = _core()
    """REST/WS refresh for SL/TP trigger price (last or mark)."""
    if c.SLTP_TRIGGER_PRICE == "mark":
        return await c._fetch_mark_price(sym)
    cached = c._last_or_mark(sym)
    if cached and cached > 0:
        return cached
    try:
        data = await c._price_feed_get("/fapi/v1/ticker/price", {"symbol": sym})
        last = float(data.get("price") or 0)
        if last > 0:
            c.latest_prices[sym] = last
            return last
    except Exception as e:
        print(f"[Price] last fetch failed {sym}: {e}")
    return await c._fetch_mark_price(sym)


async def _refresh_open_position_marks():
    c = _core()
    """Keep mark prices fresh for open paper positions (exchange triggers on mark)."""
    import time as _time_mod
    now = _time_mod.monotonic()
    symbols = sorted({
        p.get("symbol")
        for p in c.state.get("open_positions", {}).values()
        if p.get("symbol")
    })
    for sym in symbols:
        if now - c._last_mark_refresh_at.get(sym, 0.0) < c._PAPER_MARK_REFRESH_INTERVAL_SEC:
            continue
        mark = await c._fetch_mark_price(sym)
        if mark:
            c._last_mark_refresh_at[sym] = now


async def _paper_simulate_entry_fill(sym: str, side: str, mark: float):
    c = _core()
    """Simulate a taker market entry: buy at ask / sell at bid, then adverse slippage."""
    bid = ask = 0.0
    try:
        book = await c._price_feed_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
        bid = float(book.get("bidPrice") or 0)
        ask = float(book.get("askPrice") or 0)
    except Exception as e:
        print(f"[Paper] bookTicker failed {sym}: {e} — using mark for entry fill")
    if bid > 0 and ask > 0:
        base = ask if side == "Long" else bid
    else:
        base = mark
    slip = c.SLIPPAGE_PCT
    if side == "Long":
        return base * (1 + slip)
    return base * (1 - slip)
