"""Quick probe of Binance futures price WS URLs (run from repo root)."""
import asyncio
import json
import time

import websockets

BOT_URL = "wss://fstream.binance.com/market/stream?streams=!miniTicker@arr/!markPrice@arr@1s"
LEGACY_TICKER = "wss://fstream.binance.com/market/stream?streams=!ticker@arr/!markPrice@arr@1s"
MARK_ONLY_WS = "wss://fstream.binance.com/market/ws/!markPrice@arr@1s"
MARK_ONLY_STREAM = "wss://fstream.binance.com/market/stream?streams=!markPrice@arr@1s"
LEGACY = "wss://fstream.binance.com/ws/!ticker@arr"


async def probe(label: str, uri: str, sec: float = 15.0) -> None:
    counts = {"msgs": 0, "ticker": 0, "mark": 0, "other": 0}
    gaps = []
    last_at = None
    t0 = time.time()
    try:
        async with websockets.connect(
            uri,
            ping_interval=None,
            ping_timeout=None,
            open_timeout=20,
            max_size=2**24,
        ) as ws:
            while time.time() - t0 < sec:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    print(f"  [{label}] recv timeout 5s (no message)")
                    continue
                now = time.time()
                if last_at is not None:
                    gaps.append(now - last_at)
                last_at = now
                counts["msgs"] += 1
                data = json.loads(raw)
                inner = data.get("data", data) if isinstance(data, dict) else data
                if isinstance(inner, list) and inner:
                    ev = inner[0].get("e", "")
                    if ev == "markPriceUpdate":
                        counts["mark"] += 1
                    elif "c" in inner[0]:
                        counts["ticker"] += 1
                    else:
                        counts["other"] += 1
                elif isinstance(inner, dict):
                    ev = inner.get("e", "")
                    if ev == "markPriceUpdate":
                        counts["mark"] += 1
                    elif "c" in inner:
                        counts["ticker"] += 1
                    else:
                        counts["other"] += 1
    except Exception as e:
        print(f"  [{label}] FAIL: {type(e).__name__}: {e}")
        return
    elapsed = time.time() - t0
    rate = counts["msgs"] / elapsed if elapsed else 0
    max_gap = max(gaps) if gaps else 0
    print(f"  [{label}] counts={counts} elapsed={elapsed:.1f}s rate={rate:.2f}/s max_gap={max_gap:.2f}s")


async def main() -> None:
    print("Bot URL (!miniTicker@arr + mark):")
    await probe("bot", BOT_URL)
    print("Deprecated (!ticker@arr + mark):")
    await probe("legacy_ticker", LEGACY_TICKER)
    print("Mark only ws path:")
    await probe("mark_ws", MARK_ONLY_WS)
    print("Mark only stream query:")
    await probe("mark_stream", MARK_ONLY_STREAM)
    print("Legacy unrouted /ws/!ticker@arr:")
    await probe("legacy", LEGACY, 8.0)


if __name__ == "__main__":
    asyncio.run(main())
