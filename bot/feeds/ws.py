"""Binance price WebSocket and tick monitor (extracted from bot.core)."""

from __future__ import annotations

import asyncio
import json

import websockets
from config import PRICE_FEED_WS_URL

def _core():
    from bot import core
    return core


def _ws_price_symbol_filter():
    c = _core()
    """Symbols to keep from !miniTicker@arr — avoids parsing thousands of rows per tick."""
    import time as _time_mod
    now = _time_mod.monotonic()
    open_count = len(c.state.get("open_positions") or {})
    cache = getattr(_ws_price_symbol_filter, "_cache", None)
    refreshed_at = getattr(_ws_price_symbol_filter, "_refreshed_at", 0.0)
    cached_open = getattr(_ws_price_symbol_filter, "_open_count", -1)
    if (
        cache is not None
        and cached_open == open_count
        and (now - refreshed_at) < c._WS_SYMBOL_CACHE_SEC
    ):
        return cache
    wanted = set(c.SCAN_SYMBOLS)
    wanted.update(c._DASHBOARD_MARKET_SYMBOLS)
    wanted.update(
        pos.get("symbol")
        for pos in c.state.get("open_positions", {}).values()
        if pos.get("symbol")
    )
    _ws_price_symbol_filter._cache = wanted
    _ws_price_symbol_filter._refreshed_at = now
    _ws_price_symbol_filter._open_count = open_count
    return wanted


def _apply_mark_price_array(rows: list):
    c = _core()
    """Update c.latest_marks from markPriceUpdate rows (same symbol filter as ticker)."""
    wanted = c._ws_price_symbol_filter()
    updated = 0
    for row in rows:
        if not isinstance(row, dict) or row.get("e") != "markPriceUpdate":
            continue
        sym = row.get("s")
        p = float(row.get("p") or 0)
        if sym and sym in wanted and p > 0:
            c.latest_marks[sym] = p
            updated += 1
    return updated


def _is_ws_last_price_row(row: dict):
    c = _core()
    """True for 24hrMiniTicker / 24hrTicker rows (!miniTicker@arr / legacy !ticker@arr)."""
    if not isinstance(row, dict) or not row.get("s"):
        return False
    ev = row.get("e")
    if ev in ("24hrMiniTicker", "24hrTicker"):
        return "c" in row
    return "c" in row and ev != "markPriceUpdate"


def _dispatch_binance_price_ws_payload(payload):
    c = _core()
    if isinstance(payload, list):
        if not payload or not isinstance(payload[0], dict):
            return
        row = payload[0]
        if row.get("e") == "markPriceUpdate":
            _apply_mark_price_array(payload)
        elif _is_ws_last_price_row(row):
            _apply_ticker_array_prices(payload)
        return
    if not isinstance(payload, dict):
        return
    if payload.get("e") == "markPriceUpdate":
        _apply_mark_price_array([payload])
    elif _is_ws_last_price_row(payload):
        _apply_ticker_array_prices([payload])


def _process_binance_price_ws_message(raw):
    c = _core()
    """Dispatch combined / single-stream Binance futures WS payloads (miniTicker + mark)."""
    if isinstance(raw, list):
        _dispatch_binance_price_ws_payload(raw)
        return
    if not isinstance(raw, dict):
        return
    if "stream" in raw and "data" in raw:
        _dispatch_binance_price_ws_payload(raw["data"])
        return
    _dispatch_binance_price_ws_payload(raw)


def _apply_ticker_array_prices(tickers: list):
    c = _core()
    wanted = c._ws_price_symbol_filter()
    updated = 0
    for t in tickers:
        sym = t.get("s")
        if sym in wanted:
            c.latest_prices[sym] = float(t["c"])
            updated += 1
    return updated


async def price_tick_monitor_loop():
    c = _core()
    """Paper SL/TP and unrealized PnL — decoupled from WS recv so recv is never blocked by _state_lock."""
    while True:
        await asyncio.sleep(c._price_tick_monitor_interval_sec())
        if not c.state.get("open_positions"):
            continue
        try:
            if not c.LIVE_MODE:
                await c._refresh_open_position_marks()
            async with c._state_lock:
                await c._check_local_position_exits_unsafe()
                c._recalculate_unrealized_pnls()
        except Exception as e:
            print(f"[Price Monitor] error: {e}")


async def binance_ws_loop():
    c = _core()
    uri = c.PRICE_FEED_WS_URL
    read_timeout = c._PRICE_WS_READ_TIMEOUT_SEC
    while True:
        try:
            # Client ping detects half-open sockets; recv loop no longer holds _state_lock (see price_tick_monitor_loop).
            async with websockets.connect(
                uri,
                ping_interval=c._PRICE_WS_PING_INTERVAL_SEC,
                ping_timeout=c._PRICE_WS_PING_TIMEOUT_SEC,
                close_timeout=10,
                open_timeout=30,
                max_size=2**24,
            ) as ws:
                print(
                    f"Connected to Binance {c.PRICE_FEED_ENV} price WS "
                    f"(!miniTicker@arr + !markPrice@arr@1s, ping {c._PRICE_WS_PING_INTERVAL_SEC}s)"
                )
                binance_ws_loop._retries = 0
                c._mark_heartbeat("price_ws")
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=read_timeout)
                    data = json.loads(msg)
                    c._mark_heartbeat("price_ws")
                    _process_binance_price_ws_message(data)

        except asyncio.TimeoutError:
            retries = getattr(binance_ws_loop, "_retries", 0)
            delay = min(30, 3 * (2 ** retries))
            print(f"WS stale: no price update for {read_timeout}s, reconnecting in {delay}s...")
            await asyncio.sleep(delay)
            binance_ws_loop._retries = retries + 1
            continue
        except Exception as e:
            retries = getattr(binance_ws_loop, "_retries", 0)
            delay = min(30, 3 * (2 ** retries))
            print(f"WS Error: {type(e).__name__}: {e!r}, reconnecting in {delay}s...")
            await asyncio.sleep(delay)
            binance_ws_loop._retries = retries + 1
            continue
        binance_ws_loop._retries = 0


async def price_poll_loop():
    c = _core()
    """REST fallback for price updates; keeps paper PnL/local SL/TP alive if WS is quiet."""
    backoff_until = 0.0
    last_error_log_at = 0.0
    while True:
        try:
            import time as _time_mod
            now = _time_mod.monotonic()
            ws_age = c._iso_age_seconds(c._last_price_ws_ok_at)
            if ws_age is not None and ws_age <= 20:
                for sym in c._DASHBOARD_MARKET_SYMBOLS:
                    if sym not in c.SCAN_SYMBOLS and not c._mark_or_last(sym):
                        await c._fetch_mark_price(sym)
                await asyncio.sleep(10)
                continue
            if now < backoff_until:
                await asyncio.sleep(min(10, max(1, backoff_until - now)))
                continue

            data = await c._price_feed_get("/fapi/v1/ticker/price")
            wanted = set(c.SCAN_SYMBOLS)
            wanted.update(c._DASHBOARD_MARKET_SYMBOLS)
            wanted.update(
                pos.get("symbol")
                for pos in c.state.get("open_positions", {}).values()
                if pos.get("symbol")
            )
            if isinstance(data, list):
                for item in data:
                    sym = item.get("symbol")
                    if sym in wanted:
                        c.latest_prices[sym] = float(item.get("price") or 0)
            elif isinstance(data, dict) and data.get("symbol"):
                sym = data["symbol"]
                if sym in wanted:
                    c.latest_prices[sym] = float(data.get("price") or 0)

            try:
                mi = await c._price_feed_get("/fapi/v1/premiumIndex")
                if isinstance(mi, list):
                    for item in mi:
                        sym = item.get("symbol")
                        if sym in wanted:
                            mk = float(item.get("markPrice") or 0)
                            if mk > 0:
                                c.latest_marks[sym] = mk
            except Exception as e:
                if now - last_error_log_at >= 30:
                    print(f"[Price Poll] premiumIndex fallback: {e}")

            if c.state.get("open_positions"):
                if not c.LIVE_MODE:
                    await c._refresh_open_position_marks()
                async with c._state_lock:
                    await c._check_local_position_exits_unsafe()
                    c._recalculate_unrealized_pnls()
            c._mark_heartbeat("price_ws")
            backoff_until = 0.0
        except Exception as e:
            import time as _time_mod
            now = _time_mod.monotonic()
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            if status_code in (418, 429):
                body = getattr(getattr(e, "response", None), "text", "") or str(e)
                c._note_binance_rate_limit(http_status=int(status_code or 429), body=body)
                backoff_until = now + 60
                if now - last_error_log_at >= 60:
                    print("[Price Poll] Binance rate limited REST fallback; backing off for 60s")
                    last_error_log_at = now
            else:
                if now - last_error_log_at >= 30:
                    print(f"[Price Poll] error: {e}")
                    last_error_log_at = now
                backoff_until = now + 15
        await asyncio.sleep(15)
