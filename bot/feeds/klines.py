"""Kline REST fetch (extracted from bot.core)."""

from __future__ import annotations

import asyncio

from config import KLINE_FETCH_CONCURRENCY, PRICE_FEED_BASE_URL

def _core():
    from bot import core
    return core



_KLINE_FETCH_SEM = asyncio.Semaphore(max(1, KLINE_FETCH_CONCURRENCY))

async def get_klines(sym, interval, limit=200):
    c = _core()
    url = (f"{c.PRICE_FEED_BASE_URL}/fapi/v1/klines"
           f"?symbol={sym}&interval={interval}&limit={limit}")
    try:
        async with _KLINE_FETCH_SEM:
            resp = await c._http_client.get(url)
    except Exception as e:
        print(f"Request error fetching {sym}: {e}")
        return []

    if resp.status_code in (418, 429):
        c._note_binance_rate_limit(http_status=resp.status_code, body=resp.text or "")
        print(f"Rate limited fetching {sym} ({resp.status_code}), backing off...")
        await asyncio.sleep(10)
        return []
    if resp.status_code >= 500:
        print(f"Binance server error {resp.status_code} fetching {sym}")
        return []
    if resp.status_code >= 400:
        print(f"Client error {resp.status_code} fetching {sym}: {resp.text[:120]}")
        return []

    try:
        data = resp.json()
    except Exception:
        print(f"Invalid JSON from Binance for {sym}")
        return []

    if not isinstance(data, list):
        if isinstance(data, dict) and "msg" in data:
            print(f"API Error fetching {sym}: {data['msg']}")
        return []

    return [[d[0], float(d[1]), float(d[2]), float(d[3]), float(d[4]), float(d[5])]
            for d in data]
