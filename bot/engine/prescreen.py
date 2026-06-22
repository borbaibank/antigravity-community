"""Ticker-first watchlist built before candle close; kline scan uses the list at hour open."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from config import (
    KLINE_PRESCREEN_ENABLED,
    KLINE_PRESCREEN_MIN_CHG_PCT,
    KLINE_PRESCREEN_RANGE_EDGE,
    KLINE_PRESCREEN_TOP_N,
)


def _core():
    from bot import core
    return core


def prescreen_slot_key(hour_slot: datetime) -> str:
    return hour_slot.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")


def _prescreen_side_mode() -> str:
    c = _core()
    mode = str(c.state.get("trade_side_mode") or "both")
    return mode if mode in {"both", "long_only", "short_only"} else "both"


def _ticker_prescreen_score(ticker: dict) -> float | None:
    """Score a symbol for the pre-close watchlist (higher = scan first at candle open)."""
    try:
        pct = float(ticker.get("priceChangePercent") or 0)
        last = float(ticker.get("lastPrice") or 0)
        low = float(ticker.get("lowPrice") or 0)
        high = float(ticker.get("highPrice") or 0)
        qvol = float(ticker.get("quoteVolume") or 0)
    except (TypeError, ValueError):
        return None
    if qvol <= 0 or last <= 0 or high <= low:
        return None

    pct_abs = abs(pct)
    if pct_abs < KLINE_PRESCREEN_MIN_CHG_PCT:
        return None

    range_pos = (last - low) / (high - low)
    side_mode = _prescreen_side_mode()

    if side_mode == "short_only":
        if range_pos > KLINE_PRESCREEN_RANGE_EDGE:
            return None
        return pct_abs * (1.0 - range_pos)
    if side_mode == "long_only":
        if range_pos < (1.0 - KLINE_PRESCREEN_RANGE_EDGE):
            return None
        return pct_abs * range_pos
    return pct_abs


def build_prescreen_watchlist(interval: str) -> list[str]:
    """Rank base-universe symbols by 24h ticker activity; return top N."""
    c = _core()
    base = c._symbols_base_universe(interval)
    if not base:
        return []

    scored: list[tuple[str, float]] = []
    for sym in base:
        ticker = c.SCAN_TICKER_BY_SYM.get(sym)
        if not ticker:
            continue
        score = _ticker_prescreen_score(ticker)
        if score is None:
            continue
        scored.append((sym, score))

    scored.sort(key=lambda item: item[1], reverse=True)
    top_n = max(1, int(KLINE_PRESCREEN_TOP_N))
    return [sym for sym, _ in scored[:top_n]]


def active_prescreen_symbols(interval: str, hour_slot: datetime) -> list[str] | None:
    """Return saved watchlist when it matches this candle scan slot."""
    if not KLINE_PRESCREEN_ENABLED:
        return None
    c = _core()
    store = c.state.get("prescreen_watchlists")
    if not isinstance(store, dict):
        return None
    entry = store.get(interval)
    if not isinstance(entry, dict):
        return None
    if entry.get("slot") != prescreen_slot_key(hour_slot):
        return None
    symbols = entry.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        return None
    return [str(s) for s in symbols if s]


async def run_prescreen_for_interval(interval: str, target_slot: datetime) -> list[str]:
    """Build ticker watchlist for the upcoming candle scan and persist in state."""
    c = _core()
    if not KLINE_PRESCREEN_ENABLED:
        return []
    if not c._interval_has_enabled_tabs(interval):
        return []
    if not c.SCAN_TICKER_BY_SYM:
        await c.fetch_scan_symbols()

    symbols = build_prescreen_watchlist(interval)
    store = c.state.setdefault("prescreen_watchlists", {})
    store[interval] = {
        "slot": prescreen_slot_key(target_slot),
        "symbols": symbols,
        "built_at": int(datetime.now(timezone.utc).timestamp()),
    }
    side_mode = _prescreen_side_mode()
    print(
        f"[Prescreen] {interval} → {len(symbols)} symbol(s) for scan "
        f"{prescreen_slot_key(target_slot)} UTC "
        f"(side={side_mode}, top≤{KLINE_PRESCREEN_TOP_N}, "
        f"|Δ24h|≥{KLINE_PRESCREEN_MIN_CHG_PCT:g}%)"
    )
    if symbols:
        preview = ", ".join(symbols[:5])
        extra = f" (+{len(symbols) - 5})" if len(symbols) > 5 else ""
        print(f"[Prescreen] {interval} watchlist: {preview}{extra}")
    return symbols


async def ensure_prescreen_for_scan(interval: str, hour_slot: datetime) -> bool:
    """Build watchlist at scan time when scheduled prescreen was missed (bot restart, downtime)."""
    if not KLINE_PRESCREEN_ENABLED:
        return False
    if active_prescreen_symbols(interval, hour_slot):
        return False
    c = _core()
    if not c._interval_has_enabled_tabs(interval):
        return False
    from config import KLINE_PRESCREEN_MINUTE

    print(
        f"[Prescreen] Catch-up for {interval} slot {prescreen_slot_key(hour_slot)} UTC "
        f"(missed minute {KLINE_PRESCREEN_MINUTE})"
    )
    await run_prescreen_for_interval(interval, hour_slot)
    await c.save_state()
    return True


async def maybe_run_kline_prescreen(now: datetime | None = None):
    """At KLINE_PRESCREEN_MINUTE UTC, build watchlists for the next candle scan."""
    c = _core()
    if not KLINE_PRESCREEN_ENABLED:
        return
    from config import KLINE_PRESCREEN_MINUTE

    now = now or datetime.now(timezone.utc)
    if now.minute != int(KLINE_PRESCREEN_MINUTE):
        return

    hour_slot = now.replace(minute=0, second=0, microsecond=0)
    guard_key = hour_slot.isoformat()
    if getattr(c, "_prescreen_guard_key", None) == guard_key:
        return
    c._prescreen_guard_key = guard_key

    target_slot = hour_slot + timedelta(hours=1)
    ran = False
    if c._interval_has_enabled_tabs("1h"):
        await run_prescreen_for_interval("1h", target_slot)
        ran = True
    if c._interval_has_enabled_tabs("4h") and target_slot.hour % 4 == 0:
        await run_prescreen_for_interval("4h", target_slot)
        ran = True
    if ran:
        await c.save_state()
