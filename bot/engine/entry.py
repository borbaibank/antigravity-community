"""Signal scan, entry execution, and entry retry queue (extracted from bot.core)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pandas as pd
import binance_live
import strategies
from bot.engine.premium_hooks import (
    build_tab17_momentum_universe,
    sort_entry_candidates as premium_sort_entry_candidates,
    tab_max_positions_override,
)
from bot.engine.signals_registry import TAB_EVALUATORS_1H, TAB_EVALUATORS_4H, evaluate_tab_signal
from config import (
    CIRCUIT_BREAKER_DAILY_LOSS,
    ENTRY_4192_MAX_RETRIES,
    ENTRY_4192_PRICE_POLL_SEC,
    ENTRY_4192_RETRY_DELAY_SEC,
    ENTRY_4192_RETRY_MAX_AGE_SEC,
    ENTRY_BUSY_BUFFER_SEC,
    ENTRY_EVAL_BUDGET_SEC,
    ENTRY_FEE_PCT,
    ENTRY_PRICE_POLL_SEC,
    ENTRY_PRICE_WAIT_MAX_SEC,
    ENTRY_MIN_PRICE_IMPROVE_PCT,
    ENTRY_STAGGER_SEC,
    ENTRY_WAIT_FOR_BETTER_PRICE,
    ENTRY_ORDER_STYLE,
    ENTRY_LIMIT_TIF,
    ENTRY_LIMIT_MAX_AGE_SEC,
    SLTP_TP_STYLE,
    KLINE_FETCH_CONCURRENCY,
    KLINE_FETCH_DELAY_SEC,
    LEVERAGE,
    MAX_ENTRY_SIGNAL_DRIFT_PCT,
    MIN_ENTRY_AVAILABLE_MARGIN,
    MAX_POSITIONS_PER_TAB,
    NOTIONAL_SIZE,
    STARTUP_ENABLED_TABS,
    SYMBOL_SCAN_LIMIT,
    TAB_TIMEFRAMES,
    USED_SETUPS_CAP,
)

def _core():
    from bot import core
    return core


async def check_invalidations_loop(
    *,
    force: bool = False,
    hour_slot: datetime | None = None,
):
    c = _core()
    """Check open positions for tab invalidation exits (opposite signal, CHoCH, max hold, etc.).

    By default only fetches klines when the position tab's candle just closed (1h every hour,
    4h at 0/4/8/12/16/20 UTC). Pass force=True on startup or manual scan to check all positions.
    """
    hour_slot = (hour_slot or datetime.now(timezone.utc)).replace(
        minute=0, second=0, microsecond=0,
    )
    keys, tasks = [], []
    total_open = len(c.state["open_positions"])
    for pos_key, pos in list(c.state["open_positions"].items()):
        tf = c.TAB_TIMEFRAMES.get(pos["tab"], "15m")
        if not force and not _interval_candle_closed_at(hour_slot, tf):
            continue
        keys.append(pos_key)
        tasks.append(c.get_klines(pos["symbol"], tf, limit=400 if tf == "4h" else 200))

    if not tasks:
        if total_open and not force:
            print(
                f"[Invalidation] Skipping {total_open} position(s) — "
                f"no tab candle closed at {hour_slot:%H:%M} UTC"
            )
        return

    skipped = total_open - len(keys)
    if skipped:
        print(
            f"[Invalidation] Checking {len(keys)} position(s), "
            f"skipped {skipped} (candle not closed for their tab TF)"
        )

    results = await asyncio.gather(*tasks, return_exceptions=True)

    for i, pos_key in enumerate(keys):
        if pos_key not in c.state["open_positions"]: continue
        pos = c.state["open_positions"][pos_key]
        ohlcv = results[i]
        if isinstance(ohlcv, Exception) or not ohlcv: continue

        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        if strategies.check_invalidations(df, pos, pos["tab"]):
            async with c._state_lock:
                await c._close_position_unsafe(pos_key, float(df["open"].iloc[-1]), "Invalidation")

def _tab_on(tab: str):
    c = _core()
    """Return True only when this tab was explicitly enabled from the dashboard."""
    return bool(c.state.get("tab_enabled", {}).get(tab, False))


def _tab_max_positions(tab_name: str):
    c = _core()
    override = tab_max_positions_override(tab_name)
    if override is not None:
        return override
    return c._effective_max_positions()


def _tab_max_side_positions(tab_name: str):
    c = _core()
    return max(1, _tab_max_positions(tab_name) // 2)


def _symbol_min_required_notional(sym: str, price_ref: float | None = None):
    c = _core()
    info = binance_live.symbol_info.get(sym, {})
    min_notional = float(info.get("min_notional") or 0)
    min_qty = float(info.get("min_qty") or 0)
    if min_qty > 0 and price_ref and price_ref > 0:
        min_notional = max(min_notional, min_qty * float(price_ref))
    return min_notional


def _entry_size_allowed(sym: str, price_ref: float):
    c = _core()
    """Skip symbols whose exchange minimum size exceeds configured order notional."""
    if not price_ref or price_ref <= 0:
        return False, "invalid price"
    order_notional = c._effective_notional_size()
    min_required = _symbol_min_required_notional(sym, price_ref)
    if min_required > order_notional + 1e-6:
        return False, (
            f"minimum notional {min_required:.2f} > order notional {order_notional:.2f}"
        )
    qty = binance_live.round_qty(sym, order_notional / price_ref)
    min_qty = float(binance_live.symbol_info.get(sym, {}).get("min_qty") or 0)
    if qty <= 0 or (min_qty > 0 and qty < min_qty - 1e-12):
        return False, f"qty {qty} below minimum {min_qty}"
    actual_notional = qty * price_ref
    if min_required > 0 and actual_notional + 1e-6 < min_required:
        return False, (
            f"rounded notional {actual_notional:.2f} < minimum {min_required:.2f}"
        )
    return True, ""


def _pending_entries_dict():
    c = _core()
    return c.state.setdefault("pending_entry_orders", {})


def _tab_pending_count(tab_name: str) -> int:
    return sum(
        1 for p in _pending_entries_dict().values()
        if p.get("tab") == tab_name
    )


def _tab_open_slot_count(tab_name: str) -> int:
    open_n = sum(
        1 for p in _core().state.get("open_positions", {}).values()
        if p.get("tab") == tab_name
    )
    return open_n + _tab_pending_count(tab_name)


def _tab_slots_remaining(tab_name: str) -> int:
    return max(0, _tab_max_positions(tab_name) - _tab_open_slot_count(tab_name))


def _tab_slot_counts_by_tab() -> dict[str, int]:
    c = _core()
    counts = {tab: 0 for tab in c.TABS}
    for p in c.state.get("open_positions", {}).values():
        tab = p.get("tab")
        if tab in counts:
            counts[tab] += 1
    for p in _pending_entries_dict().values():
        tab = p.get("tab")
        if tab in counts:
            counts[tab] += 1
    return counts


def _tab_side_counts(tab_name: str):
    c = _core()
    long_count = 0
    short_count = 0
    for pos in c.state.get("open_positions", {}).values():
        if pos.get("tab") != tab_name:
            continue
        side = str(pos.get("side") or "").lower()
        if side == "long":
            long_count += 1
        elif side == "short":
            short_count += 1
    for pending in _pending_entries_dict().values():
        if pending.get("tab") != tab_name:
            continue
        side = str(pending.get("side") or "").lower()
        if side == "long":
            long_count += 1
        elif side == "short":
            short_count += 1
    return long_count, short_count


def _balanced_candidate_allowed(
    side: str, long_count: int, short_count: int, total_count: int, tab_name: str,
):
    c = _core()
    max_total = _tab_max_positions(tab_name)
    max_side = max(1, max_total // 2)
    if total_count >= max_total:
        return False
    side = str(side or "")
    if side == "Long":
        if long_count >= max_side:
            return False
        return abs((long_count + 1) - short_count) <= 1
    if side == "Short":
        if short_count >= max_side:
            return False
        return abs(long_count - (short_count + 1)) <= 1
    return False


def _side_cap_candidate_allowed(
    side: str, long_count: int, short_count: int, total_count: int, tab_name: str,
):
    c = _core()
    max_total = _tab_max_positions(tab_name)
    if total_count >= max_total:
        return False
    max_side = max(1, max_total // 2)
    if side == "Long":
        return long_count < max_side
    if side == "Short":
        return short_count < max_side
    return False


def _entry_long_short_balance_allowed(tab_name: str, side: str):
    c = _core()
    """Gate execute_entry (and retries) with the same rules as _open_balanced_candidates."""
    mode = c._effective_long_short_balance_mode(tab_name)
    if mode == "off":
        return True, ""
    long_count, short_count = _tab_side_counts(tab_name)
    total_count = long_count + short_count
    side_s = str(side or "")
    max_total = _tab_max_positions(tab_name)
    max_side = max(1, max_total // 2)
    if mode == "cap":
        if _side_cap_candidate_allowed(side_s, long_count, short_count, total_count, tab_name):
            return True, ""
        if total_count >= max_total:
            return False, f"max positions ({max_total}) reached"
        if side_s == "Long" and long_count >= max_side:
            return False, f"50/50 cap: long {long_count}/{max_side}"
        if side_s == "Short" and short_count >= max_side:
            return False, f"50/50 cap: short {short_count}/{max_side}"
        return False, "50/50 cap: side limit"
    if mode == "nearly":
        if _balanced_candidate_allowed(side_s, long_count, short_count, total_count, tab_name):
            return True, ""
        if total_count >= max_total:
            return False, f"max positions ({max_total}) reached"
        return False, (
            f"nearly balance: long={long_count} short={short_count} "
            f"(max {max_total}, side cap {max_side})"
        )
    return True, ""


def _trade_side_allowed(side: str, tab_name: str):
    c = _core()
    mode = c._effective_trade_side_mode(tab_name)
    if mode == "long_only" and side == "Short":
        return False, "trade side mode is Long Only"
    if mode == "short_only" and side == "Long":
        return False, "trade side mode is Short Only"
    return True, ""


async def _queue_entry_retries(tab_name: str, items: list[dict], *, attempt: int = 1):
    c = _core()
    if not c.LIVE_MODE or not items:
        return
    if attempt > c.ENTRY_4192_MAX_RETRIES:
        syms = ", ".join(item["sym"] for item in items[:5])
        extra = f" (+{len(items) - 5} more)" if len(items) > 5 else ""
        print(f"[Entry Retry] Max retries ({c.ENTRY_4192_MAX_RETRIES}) — drop {tab_name}: {syms}{extra}")
        return
    import time
    retry_at = time.monotonic() + c.ENTRY_4192_RETRY_DELAY_SEC
    existing_keys = {r["setup_key"] for r in c._pending_entry_retries}
    queued = 0
    for item in items:
        setup_key = item["setup_key"]
        if setup_key in existing_keys:
            continue
        if setup_key in c.state.get("used_setups", []):
            continue
        pos_key = f"{item['sym']}_{tab_name}"
        if pos_key in c.state.get("open_positions", {}):
            continue
        signal_ts_ms = item.get("signal_ts_ms") or _setup_signal_ts_ms(setup_key)
        expired, age_sec = _entry_retry_signal_expired(tab_name, signal_ts_ms)
        if expired:
            print(
                f"[Entry Retry] Skip {item['sym']} {tab_name}: signal age {age_sec:.0f}s "
                f"> {c.ENTRY_4192_RETRY_MAX_AGE_SEC}s (expired)"
            )
            continue
        sig = item["sig"]
        c._pending_entry_retries.append({
            "tab_name": tab_name,
            "sym": item["sym"],
            "sig": sig,
            "setup_key": setup_key,
            "signal_ep": float(sig.get("ep") or 0),
            "signal_ts_ms": signal_ts_ms,
            "retry_at_mono": retry_at,
            "attempt": attempt,
            "queue_kind": "4192",
        })
        existing_keys.add(setup_key)
        queued += 1
    if queued:
        print(
            f"[Entry Retry] Queued {queued} {tab_name} candidate(s) in {c.ENTRY_4192_RETRY_DELAY_SEC}s "
            f"(attempt {attempt}, -4192); then enter when mark at-or-better than signal ep"
        )


async def _halt_entry_batch_on_cooling_off(tab_name: str, candidates: list[dict], start_idx: int):
    c = _core()
    if start_idx >= len(candidates):
        return
    remaining = candidates[start_idx:]
    await _queue_entry_retries(tab_name, remaining)
    print(f"[Entry] Batch halted — Binance -4192; {len(remaining)} queued for retry")


async def _queue_price_wait_entry(
    sym: str,
    sig: dict,
    tab_name: str,
    setup_key: str,
):
    c = _core()
    """Defer entry until mark is at-or-better than signal ep. Returns True if queued."""
    if not c.ENTRY_WAIT_FOR_BETTER_PRICE or not setup_key:
        return False
    import time
    existing_keys = {r["setup_key"] for r in c._pending_entry_retries}
    if setup_key in existing_keys:
        return True
    if setup_key in c.state.get("used_setups", []):
        return True
    pos_key = f"{sym}_{tab_name}"
    if pos_key in c.state.get("open_positions", {}):
        return True
    signal_ts_ms = _setup_signal_ts_ms(setup_key)
    expired, age_sec = _entry_price_wait_expired(tab_name, signal_ts_ms)
    if expired:
        print(
            f"[Entry Wait] Skip {sym} {tab_name}: signal age {age_sec:.0f}s "
            f"> {c.ENTRY_PRICE_WAIT_MAX_SEC}s (expired)"
        )
        if setup_key not in c.state.get("used_setups", []):
            c.state["used_setups"].append(setup_key)
            await c.save_state()
        return False
    signal_ep = float(sig.get("ep") or 0)
    c._pending_entry_retries.append({
        "tab_name": tab_name,
        "sym": sym,
        "sig": sig,
        "setup_key": setup_key,
        "signal_ep": signal_ep,
        "signal_ts_ms": signal_ts_ms,
        "retry_at_mono": time.monotonic(),
        "attempt": 0,
        "queue_kind": "price_wait",
    })
    mark = c._entry_reference_price(sym)
    mark_s = f"{mark:.4f}" if mark else "n/a"
    px_label = "last" if c.ENTRY_TRIGGER_PRICE == "last" else "mark"
    cmp_word = "<=" if (sig.get("side") or "").strip() == "Long" else ">="
    print(
        f"[Entry Wait] Deferred {sym} {tab_name} {sig.get('side')}: {px_label}={mark_s} "
        f"need {cmp_word} ep={signal_ep:.4f}"
    )
    return True


async def _defer_entry_until_better_price(
    sym: str,
    sig: dict,
    tab_name: str,
    setup_key: str | None,
):
    c = _core()
    """Return True when entry is deferred to the price-wait queue."""
    if not c.ENTRY_WAIT_FOR_BETTER_PRICE:
        return False
    if not setup_key:
        return False
    signal_ep = float(sig.get("ep") or 0)
    if signal_ep <= 0:
        return False
    mark = await c._fetch_entry_reference_price(sym)
    if not mark or mark <= 0:
        mark = c._entry_reference_price(sym)
    if not mark or mark <= 0:
        return False
    if _entry_price_at_or_better(sig.get("side") or "", mark, signal_ep):
        return False
    return await _queue_price_wait_entry(sym, sig, tab_name, setup_key)


def _setup_signal_ts_ms(setup_key: str):
    c = _core()
    try:
        return int(str(setup_key).rsplit("_", 1)[-1])
    except (ValueError, IndexError):
        return None


def _signal_candle_close_ms(signal_ts_ms: int, tab_name: str):
    c = _core()
    interval = c.TAB_TIMEFRAMES.get(tab_name, "1h")
    return int(signal_ts_ms) + _interval_hours(interval) * 3600 * 1000


def _entry_retry_signal_expired(tab_name: str, signal_ts_ms: int | None):
    c = _core()
    if not signal_ts_ms:
        return False, 0.0
    import time
    close_ms = _signal_candle_close_ms(signal_ts_ms, tab_name)
    age_sec = (time.time() * 1000 - close_ms) / 1000.0
    return age_sec > c.ENTRY_4192_RETRY_MAX_AGE_SEC, age_sec


def _entry_price_wait_expired(tab_name: str, signal_ts_ms: int | None):
    c = _core()
    if not signal_ts_ms:
        return False, 0.0
    import time
    close_ms = _signal_candle_close_ms(signal_ts_ms, tab_name)
    age_sec = (time.time() * 1000 - close_ms) / 1000.0
    return age_sec > c.ENTRY_PRICE_WAIT_MAX_SEC, age_sec


def _entry_price_at_or_better(side: str, mark: float, signal_ep: float):
    c = _core()
    """Long: mark <= ep (minus min improve); Short: mark >= ep (plus min improve)."""
    if signal_ep <= 0 or mark <= 0:
        return True
    improve = float(c.ENTRY_MIN_PRICE_IMPROVE_PCT or 0)
    s = (side or "").strip()
    if s == "Long":
        threshold = signal_ep * (1.0 - improve) if improve > 0 else signal_ep
        return mark <= threshold
    if s == "Short":
        threshold = signal_ep * (1.0 + improve) if improve > 0 else signal_ep
        return mark >= threshold
    return True


def _entry_price_favorability_pct(side: str, mark: float, signal_ep: float):
    c = _core()
    """How much better than ep (fraction of ep). Higher = more favorable; used to sort ready queue."""
    if signal_ep <= 0 or mark <= 0:
        return 0.0
    s = (side or "").strip()
    if s == "Long":
        return (signal_ep - mark) / signal_ep
    if s == "Short":
        return (mark - signal_ep) / signal_ep
    return 0.0


def _retry_item_price_ready(retry_item: dict):
    c = _core()
    sig = retry_item.get("sig") or {}
    signal_ep = float(retry_item.get("signal_ep") or sig.get("ep") or 0)
    mark = c._entry_reference_price(retry_item.get("sym"))
    if not mark:
        return False
    return _entry_price_at_or_better(sig.get("side") or "", mark, signal_ep)


def _retry_item_favorability(retry_item: dict):
    c = _core()
    sig = retry_item.get("sig") or {}
    signal_ep = float(retry_item.get("signal_ep") or sig.get("ep") or 0)
    mark = c._entry_reference_price(retry_item.get("sym")) or 0.0
    return _entry_price_favorability_pct(sig.get("side") or "", mark, signal_ep)


def _partition_due_entry_retries(due_items: list[dict]):
    c = _core()
    """Split due retries into price-ready (sorted best-first) and deferred (not ready / no mark)."""
    ready: list[dict] = []
    waiting: list[dict] = []
    for retry_item in due_items:
        tab_name = retry_item["tab_name"]
        sym = retry_item["sym"]
        setup_key = retry_item["setup_key"]
        if setup_key in c.state.get("used_setups", []):
            print(f"[Entry Retry] Skip {sym} {tab_name}: setup already used")
            continue
        if f"{sym}_{tab_name}" in c.state.get("open_positions", {}):
            print(f"[Entry Retry] Skip {sym} {tab_name}: already open")
            continue
        signal_ts_ms = retry_item.get("signal_ts_ms") or _setup_signal_ts_ms(setup_key)
        queue_kind = retry_item.get("queue_kind", "4192")
        if queue_kind == "price_wait":
            expired, age_sec = _entry_price_wait_expired(tab_name, signal_ts_ms)
            if expired:
                print(
                    f"[Entry Wait] Skip {sym} {tab_name}: signal age {age_sec:.0f}s "
                    f"> {c.ENTRY_PRICE_WAIT_MAX_SEC}s (timeout, no entry)"
                )
                if setup_key not in c.state.get("used_setups", []):
                    c.state["used_setups"].append(setup_key)
                continue
        else:
            expired, age_sec = _entry_retry_signal_expired(tab_name, signal_ts_ms)
            if expired:
                print(
                    f"[Entry Retry] Skip {sym} {tab_name}: signal age {age_sec:.0f}s "
                    f"> {c.ENTRY_4192_RETRY_MAX_AGE_SEC}s (expired)"
                )
                continue
        if _retry_item_price_ready(retry_item):
            ready.append(retry_item)
        else:
            waiting.append(retry_item)
    ready.sort(key=_retry_item_favorability, reverse=True)
    return ready, waiting


def _requeue_deferred_entry_retries(items: list[dict]):
    c = _core()
    """Put not-ready items back on the queue without burning a -4192 attempt."""
    if not items:
        return
    import time
    existing_keys = {r["setup_key"] for r in c._pending_entry_retries}
    requeued = 0
    for item in items:
        setup_key = item["setup_key"]
        if setup_key in existing_keys:
            continue
        deferred = dict(item)
        poll_sec = (
            c.ENTRY_PRICE_POLL_SEC
            if item.get("queue_kind") == "price_wait"
            else c.ENTRY_4192_PRICE_POLL_SEC
        )
        deferred["retry_at_mono"] = time.monotonic() + poll_sec
        c._pending_entry_retries.append(deferred)
        existing_keys.add(setup_key)
        requeued += 1
    if requeued:
        syms = ", ".join(item["sym"] for item in items[:5])
        extra = f" (+{len(items) - 5} more)" if len(items) > 5 else ""
        price_wait = any(item.get("queue_kind") == "price_wait" for item in items)
        tag = "Entry Wait" if price_wait else "Entry Retry"
        poll_sec = c.ENTRY_PRICE_POLL_SEC if price_wait else c.ENTRY_4192_PRICE_POLL_SEC
        print(
            f"[{tag}] Deferred {requeued} until price at-or-better ep "
            f"(retry in {poll_sec:g}s): {syms}{extra}"
        )


async def _wait_for_entry_price_at_or_better(
    sym: str,
    tab_name: str,
    side: str,
    signal_ep: float,
    signal_ts_ms: int | None,
):
    c = _core()
    """Poll until mark is at-or-better than signal ep, or signal expires. Returns True if ready."""
    import time
    last_log_at = 0.0
    cmp_word = "<=" if (side or "").strip() == "Long" else ">="
    while True:
        expired, age_sec = _entry_retry_signal_expired(tab_name, signal_ts_ms)
        if expired:
            print(
                f"[Entry Retry] Skip {sym} {tab_name}: signal age {age_sec:.0f}s "
                f"> {c.ENTRY_4192_RETRY_MAX_AGE_SEC}s while waiting for price"
            )
            return False
        mark = c._entry_reference_price(sym)
        if mark and _entry_price_at_or_better(side, mark, signal_ep):
            return True
        now = time.monotonic()
        if now - last_log_at >= 30.0:
            mark_s = f"{mark:.4f}" if mark else "n/a"
            px_label = "last" if c.ENTRY_TRIGGER_PRICE == "last" else "mark"
            print(
                f"[Entry Retry] Wait {sym} {tab_name} {side}: {px_label}={mark_s} "
                f"need {cmp_word} ep={signal_ep:.4f}"
            )
            last_log_at = now
        await asyncio.sleep(c.ENTRY_4192_PRICE_POLL_SEC)


async def _retry_one_queued_entry(retry_item: dict, *, block_until_price: bool = False):
    c = _core()
    sym = retry_item["sym"]
    tab_name = retry_item["tab_name"]
    sig = retry_item["sig"]
    setup_key = retry_item["setup_key"]
    signal_ep = float(retry_item.get("signal_ep") or sig.get("ep") or 0)
    attempt = int(retry_item.get("attempt") or 1)

    if setup_key in c.state.get("used_setups", []):
        print(f"[Entry Retry] Skip {sym} {tab_name}: setup already used")
        return None
    pos_key = f"{sym}_{tab_name}"
    if pos_key in c.state.get("open_positions", {}):
        print(f"[Entry Retry] Skip {sym} {tab_name}: already open")
        return None

    signal_ts_ms = retry_item.get("signal_ts_ms") or _setup_signal_ts_ms(setup_key)
    expired, age_sec = _entry_retry_signal_expired(tab_name, signal_ts_ms)
    if expired:
        print(
            f"[Entry Retry] Skip {sym} {tab_name}: signal age {age_sec:.0f}s "
            f"> {c.ENTRY_4192_RETRY_MAX_AGE_SEC}s (expired)"
        )
        return None

    side = sig.get("side") or ""
    if block_until_price:
        if not await _wait_for_entry_price_at_or_better(sym, tab_name, side, signal_ep, signal_ts_ms):
            return None
    elif not _retry_item_price_ready(retry_item):
        return "not_ready"

    await _stagger_before_next_entry()
    mark = c._entry_reference_price(sym)
    mark_s = f"{mark:.4f}" if mark else "n/a"
    px_label = "last" if c.ENTRY_TRIGGER_PRICE == "last" else "mark"
    queue_kind = retry_item.get("queue_kind", "4192")
    if queue_kind == "price_wait":
        print(
            f"[Entry Wait] {sym} {tab_name} ready "
            f"(signal ep={signal_ep:.4f} {px_label}={mark_s})"
        )
    else:
        print(
            f"[Entry Retry] {sym} {tab_name} attempt {attempt} "
            f"(signal ep={signal_ep:.4f} {px_label}={mark_s})"
        )
    result = await c.execute_entry(sym, sig, tab_name, setup_key=setup_key, skip_price_wait=True)
    if result == "cooling_off":
        await _queue_entry_retries(
            tab_name,
            [{"sym": sym, "sig": sig, "setup_key": setup_key}],
            attempt=attempt + 1,
        )
        return "cooling_off"
    if pos_key in c.state.get("open_positions", {}):
        if setup_key not in c.state["used_setups"]:
            c.state["used_setups"].append(setup_key)
            await c.save_state()
        print(
            f"[ENTRY | RETRY OK] {c._utc_log_stamp()}  {tab_name}  {sym}  {sig.get('side')}  "
            f"(see entry block above if filled)"
        )
    return result


async def _process_due_entry_retries():
    c = _core()
    import time
    now = time.monotonic()
    due_idx = [i for i, r in enumerate(c._pending_entry_retries) if r["retry_at_mono"] <= now]
    if not due_idx:
        return
    due_items = [c._pending_entry_retries[i] for i in due_idx]
    for i in reversed(due_idx):
        c._pending_entry_retries.pop(i)

    used_before = len(c.state.get("used_setups", []))
    ready, waiting = _partition_due_entry_retries(due_items)
    if len(c.state.get("used_setups", [])) > used_before:
        await c.save_state()
    _requeue_deferred_entry_retries(waiting)

    for idx, retry_item in enumerate(ready):
        result = await _retry_one_queued_entry(retry_item)
        if result == "cooling_off":
            unprocessed_ready = ready[idx + 1:]
            tail = [
                {
                    "sym": r["sym"],
                    "sig": r["sig"],
                    "setup_key": r["setup_key"],
                    "signal_ts_ms": r.get("signal_ts_ms"),
                }
                for r in unprocessed_ready
            ]
            if tail:
                await _queue_entry_retries(retry_item["tab_name"], tail, attempt=1)
            break


async def entry_retry_loop():
    c = _core()
    while True:
        try:
            await _process_due_entry_retries()
        except Exception as e:
            print(f"[Entry Retry] loop error: {e}")
        await asyncio.sleep(5)


async def _open_balanced_candidates(tab_name: str, candidates: list[dict], tab_counts: dict[str, int]):
    c = _core()
    if not candidates:
        return

    balance_mode = c._effective_long_short_balance_mode(tab_name)
    if balance_mode == "off":
        for i, item in enumerate(candidates):
            if _tab_slots_remaining(tab_name) <= 0:
                break
            setup_key = item["setup_key"]
            if setup_key in c.state["used_setups"]:
                continue
            sym = item["sym"]
            sig = item["sig"]
            await _stagger_before_next_entry()
            result = await c.execute_entry(sym, sig, tab_name, setup_key=setup_key)
            if result == "cooling_off":
                await _halt_entry_batch_on_cooling_off(tab_name, candidates, i)
                break
            if result == "price_wait":
                continue
            if result == "limit_placed":
                c.state["used_setups"].append(setup_key)
                tab_counts[tab_name] = tab_counts.get(tab_name, 0) + 1
                print(f"[{tab_name}] Limit entry {sig.get('side')} {sym}; Balance OFF Total={tab_counts[tab_name]}")
                continue
            if f"{sym}_{tab_name}" in c.state.get("open_positions", {}):
                c.state["used_setups"].append(setup_key)
                tab_counts[tab_name] = tab_counts.get(tab_name, 0) + 1
                print(f"[{tab_name}] Opened {sig.get('side')} {sym}; Balance OFF Total={tab_counts[tab_name]}")
        return

    if balance_mode == "cap":
        long_count, short_count = _tab_side_counts(tab_name)
        total_count = tab_counts.get(tab_name, long_count + short_count)
        for i, item in enumerate(candidates):
            if _tab_slots_remaining(tab_name) <= 0:
                break
            sig = item["sig"]
            if not _side_cap_candidate_allowed(sig.get("side"), long_count, short_count, total_count, tab_name):
                continue
            setup_key = item["setup_key"]
            if setup_key in c.state["used_setups"]:
                continue
            sym = item["sym"]
            await _stagger_before_next_entry()
            result = await c.execute_entry(sym, sig, tab_name, setup_key=setup_key)
            if result == "cooling_off":
                await _halt_entry_batch_on_cooling_off(tab_name, candidates, i)
                break
            if result == "price_wait":
                continue
            if result == "limit_placed":
                c.state["used_setups"].append(setup_key)
                if sig.get("side") == "Long":
                    long_count += 1
                elif sig.get("side") == "Short":
                    short_count += 1
                total_count = tab_counts[tab_name] = tab_counts.get(tab_name, 0) + 1
                print(f"[{tab_name} 50/50 Cap] Limit {sig.get('side')} {sym}; Long={long_count} Short={short_count} Total={total_count}")
                continue
            if f"{sym}_{tab_name}" in c.state.get("open_positions", {}):
                c.state["used_setups"].append(setup_key)
                if sig.get("side") == "Long":
                    long_count += 1
                elif sig.get("side") == "Short":
                    short_count += 1
                total_count = tab_counts[tab_name] = tab_counts.get(tab_name, 0) + 1
                print(f"[{tab_name} 50/50 Cap] Opened {sig.get('side')} {sym}; Long={long_count} Short={short_count} Total={total_count}")
        return

    long_count, short_count = _tab_side_counts(tab_name)
    total_count = tab_counts.get(tab_name, long_count + short_count)
    remaining = list(candidates)

    while remaining and _tab_slots_remaining(tab_name) > 0:
        if long_count >= _tab_max_side_positions(tab_name) and short_count >= _tab_max_side_positions(tab_name):
            break

        preferred_side = "Short" if long_count > short_count else ("Long" if short_count > long_count else None)

        def pick_index(side_filter: str | None = None) -> int | None:
            for idx, item in enumerate(remaining):
                side = item["sig"].get("side")
                if side_filter and side != side_filter:
                    continue
                if _balanced_candidate_allowed(side, long_count, short_count, total_count, tab_name):
                    return idx
            return None

        chosen_idx = pick_index(preferred_side) if preferred_side else pick_index()
        if chosen_idx is None and preferred_side:
            chosen_idx = pick_index()
        if chosen_idx is None:
            break

        item = remaining.pop(chosen_idx)
        sym = item["sym"]
        sig = item["sig"]
        setup_key = item["setup_key"]
        await _stagger_before_next_entry()
        result = await c.execute_entry(sym, sig, tab_name, setup_key=setup_key)
        if result == "cooling_off":
            await _queue_entry_retries(tab_name, [item] + remaining)
            print(f"[Entry] Batch halted — Binance -4192; {1 + len(remaining)} queued for retry")
            break
        if result == "price_wait":
            continue
        if result == "limit_placed":
            c.state["used_setups"].append(setup_key)
            if sig.get("side") == "Long":
                long_count += 1
            elif sig.get("side") == "Short":
                short_count += 1
            total_count = tab_counts[tab_name] = tab_counts.get(tab_name, 0) + 1
            print(f"[{tab_name} Balance] Limit {sig.get('side')} {sym}; Long={long_count} Short={short_count} Total={total_count}")
            continue

        pos_key = f"{sym}_{tab_name}"
        if pos_key in c.state.get("open_positions", {}):
            c.state["used_setups"].append(setup_key)
            if sig.get("side") == "Long":
                long_count += 1
            elif sig.get("side") == "Short":
                short_count += 1
            total_count = tab_counts[tab_name] = tab_counts.get(tab_name, 0) + 1
            print(f"[{tab_name} Balance] Opened {sig.get('side')} {sym}; Long={long_count} Short={short_count} Total={total_count}")


def _interval_hours(interval: str):
    c = _core()
    value = str(interval or "").lower()
    if value.endswith("h"):
        try:
            return max(1, int(value[:-1]))
        except ValueError:
            return 1
    return 1


def _interval_candle_closed_at(hour_slot: datetime, interval: str):
    c = _core()
    """True when hour_slot UTC is a candle close boundary for interval (e.g. 4h at 0,4,8…)."""
    hours = _interval_hours(interval)
    if hours <= 1:
        return True
    return hour_slot.astimezone(timezone.utc).hour % hours == 0


def _next_interval_boundary_after(dt: datetime, interval: str):
    c = _core()
    """Return the next UTC candle boundary after bot start for the interval."""
    dt = dt.astimezone(timezone.utc)
    hours = _interval_hours(interval)
    base = dt.replace(minute=0, second=0, microsecond=0)
    if hours <= 1:
        return base + timedelta(hours=1)
    boundary_hour = (base.hour // hours) * hours
    boundary = base.replace(hour=boundary_hour)
    return boundary + timedelta(hours=hours)


def _scan_gate_opens_at(interval: str):
    c = _core()
    return _next_interval_boundary_after(c._BOT_STARTED_AT, interval) + timedelta(
        seconds=c._effective_kline_fetch_delay_sec()
    )


def _entry_gate_opens_at(interval: str):
    c = _core()
    return _scan_gate_opens_at(interval)


def _scan_gate_open(interval: str, now: datetime | None = None):
    c = _core()
    now = now or datetime.now(timezone.utc)
    return now >= _scan_gate_opens_at(interval)


def _entry_gate_open(interval: str, now: datetime | None = None):
    c = _core()
    now = now or datetime.now(timezone.utc)
    return now >= _entry_gate_opens_at(interval)


def _entry_gate_status(interval: str):
    c = _core()
    scan_at = _scan_gate_opens_at(interval)
    entry_at = _entry_gate_opens_at(interval)
    return (
        f"bot started {c._BOT_STARTED_AT:%Y-%m-%d %H:%M:%S} UTC; "
        f"{interval} kline scan at {scan_at:%Y-%m-%d %H:%M:%S} UTC; "
        f"entries at {entry_at:%Y-%m-%d %H:%M:%S} UTC"
    )


def _seconds_until_next_candle_eval():
    c = _core()
    """Seconds until next scheduled kline scan (+ entries immediately after)."""
    now = datetime.now(timezone.utc)
    hour_slot = now.replace(minute=0, second=0, microsecond=0)
    delay = c._effective_kline_fetch_delay_sec()
    scan_target = hour_slot + timedelta(seconds=delay)
    if now >= scan_target:
        hour_slot += timedelta(hours=1)
        scan_target = hour_slot + timedelta(seconds=delay)
    return max(0.0, (scan_target - now).total_seconds())


def _candle_scan_at(hour_slot: datetime):
    c = _core()
    return hour_slot + timedelta(seconds=c._effective_kline_fetch_delay_sec())


def _next_scheduler_wake_at(now: datetime, last_scan_slot: datetime | None):
    c = _core()
    hour_slot = now.replace(minute=0, second=0, microsecond=0)
    scan_at = _candle_scan_at(hour_slot)
    candidates: list[datetime] = []
    if last_scan_slot != hour_slot and now < scan_at:
        candidates.append(scan_at)
    next_hour = hour_slot + timedelta(hours=1)
    candidates.append(_candle_scan_at(next_hour))
    return min(candidates)


def _reset_entry_stagger():
    c = _core()
    c._ENTRY_STAGGER_ARMED = False


async def _stagger_before_next_entry():
    c = _core()
    """Wait c.ENTRY_STAGGER_SEC between successive entry attempts in one eval cycle."""
    if c._ENTRY_STAGGER_ARMED:
        await asyncio.sleep(c.ENTRY_STAGGER_SEC)
    c._ENTRY_STAGGER_ARMED = True


def _mark_entry_busy(duration_sec: float):
    c = _core()
    """Extend the entry window — PnL userTrades work waits until it clears."""
    import time as _time_mod
    until = _time_mod.monotonic() + max(0.0, float(duration_sec))
    c._entry_busy_until_mono = max(c._entry_busy_until_mono, until)


def _begin_entry_window():
    c = _core()
    import time as _time_mod
    c._entry_busy_until_mono = _time_mod.monotonic() + c.ENTRY_EVAL_BUDGET_SEC


def _release_entry_busy_after_eval():
    c = _core()
    """After eval finishes, keep REST clear for live entries a little longer."""
    import time as _time_mod
    c._entry_busy_until_mono = _time_mod.monotonic() + c.ENTRY_BUSY_BUFFER_SEC


def _entry_window_active():
    c = _core()
    import time as _time_mod
    return _time_mod.monotonic() < c._entry_busy_until_mono


async def _await_entry_window_clear(label: str = "userTrades"):
    c = _core()
    """Block until candle eval / startup entry window ends."""
    import time as _time_mod
    while _entry_window_active():
        now_mono = _time_mod.monotonic()
        if label and (now_mono - c._entry_busy_defer_log_at) >= 30.0:
            remaining = max(0.0, c._entry_busy_until_mono - now_mono)
            print(f"[PnL Defer] Pausing {label} — entry window active ({remaining:.0f}s remaining)")
            c._entry_busy_defer_log_at = now_mono
        await asyncio.sleep(PNL_REPAIR_DEFER_POLL_SEC)


async def scan_candle_signals(interval: str, *, hour_slot=None):
    c = _core()
    """Fetch klines and collect entry candidates; does not place orders."""
    from datetime import datetime, timezone

    if hour_slot is None:
        hour_slot = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    if c.LIVE_MODE and c._binance_rate_limited():
        snap = c._binance_rate_limit_snapshot()
        print(
            f"[API Ban] Skipping {interval} signal scan "
            f"({snap.get('remaining_sec', 0)}s remaining)"
        )
        return None
    if not _scan_gate_open(interval):
        if interval not in c._ENTRY_GATE_LOGGED:
            print(f"[Entry Gate] Skipping {interval} scan until gate opens ({_entry_gate_status(interval)})")
            c._ENTRY_GATE_LOGGED.add(interval)
        return None
    if not c._interval_has_enabled_tabs(interval):
        print(f"[Scan] Skipping {interval} — no enabled tabs on this timeframe")
        return None

    from bot.engine.prescreen import ensure_prescreen_for_scan

    await ensure_prescreen_for_scan(interval, hour_slot)

    symbols = c._symbols_for_interval_scan(interval, hour_slot)
    if not symbols:
        print(f"[Scan] Skipping {interval} — empty symbol universe")
        return None

    base_n = len(c._symbols_base_universe(interval))
    if len(symbols) < base_n:
        print(
            f"Scanning {interval} signals (prescreen watchlist, "
            f"{len(symbols)}/{base_n} symbols, kline fetch)..."
        )
    else:
        print(f"Scanning {interval} signals (kline fetch, {len(symbols)} symbols)...")

    tab_counts = _tab_slot_counts_by_tab()
    candidates_by_tab = {tab: [] for tab in c.TABS}

    kline_limit = 400 if interval == "4h" else 250
    import time as _time_mod
    fetch_started = _time_mod.monotonic()
    tasks = [c.get_klines(sym, interval, limit=kline_limit) for sym in symbols]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    fetch_sec = _time_mod.monotonic() - fetch_started
    weight_est = len(symbols) * c._kline_request_weight(kline_limit)
    print(
        f"[Scan] {interval} fetched {len(symbols)} klines in {fetch_sec:.1f}s "
        f"(~{weight_est} REST weight est., concurrency≤{KLINE_FETCH_CONCURRENCY})"
    )

    def collect_candidate(
        tab_name: str,
        sym: str,
        signal_ts: int,
        sig: dict | None,
        *,
        momentum_score: float | None = None,
    ) -> None:
        if not sig:
            return
        side_ok, _ = _trade_side_allowed(sig.get("side"), tab_name)
        if not side_ok:
            return
        setup_key = f"{sym}_{tab_name}_{signal_ts}"
        item = {"sym": sym, "sig": sig, "setup_key": setup_key}
        if momentum_score is not None:
            item["momentum_score"] = momentum_score
        candidates_by_tab.setdefault(tab_name, []).append(item)

    def can_collect(tab_name: str, sym: str, signal_ts: int) -> bool:
        setup_key = f"{sym}_{tab_name}_{signal_ts}"
        pos_key = f"{sym}_{tab_name}"
        pending = _pending_entries_dict()
        return (
            _tab_on(tab_name)
            and pos_key not in c.state["open_positions"]
            and pos_key not in pending
            and _tab_slots_remaining(tab_name) > 0
            and tab_counts.get(tab_name, 0) < _tab_max_positions(tab_name)
            and setup_key not in c.state["used_setups"]
        )

    tab17_universe: dict[str, float] = {}
    if interval == "1h" and _tab_on("Tab17"):
        tab17_universe = build_tab17_momentum_universe(symbols, results)

    for i, sym in enumerate(symbols):
        try:
            ohlcv = results[i]
            if isinstance(ohlcv, Exception) or not ohlcv:
                continue
            df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
            signal_ts = int(df["timestamp"].iloc[-2])


            if interval == "4h":
                for tab_name, evaluator, post in TAB_EVALUATORS_4H:
                    if can_collect(tab_name, sym, signal_ts):
                        collect_candidate(
                            tab_name,
                            sym,
                            signal_ts,
                            evaluate_tab_signal(tab_name, evaluator, post, df),
                        )

            if interval == "1h":
                for tab_name, evaluator, post in TAB_EVALUATORS_1H:
                    if post == "tab17_momentum":
                        if not (tab17_universe and sym in tab17_universe and can_collect(tab_name, sym, signal_ts)):
                            continue
                        collect_candidate(
                            tab_name,
                            sym,
                            signal_ts,
                            evaluate_tab_signal(tab_name, evaluator, None, df),
                            momentum_score=tab17_universe[sym],
                        )
                    elif can_collect(tab_name, sym, signal_ts):
                        collect_candidate(
                            tab_name,
                            sym,
                            signal_ts,
                            evaluate_tab_signal(tab_name, evaluator, post, df),
                        )


            if len(c.state["used_setups"]) > USED_SETUPS_CAP:
                c.state["used_setups"] = c.state["used_setups"][-USED_SETUPS_CAP:]

        except Exception as e:
            print(f"Error checking signal for {sym} ({interval}): {e}")

    total = sum(len(v) for v in candidates_by_tab.values())
    print(f"[Scan] {interval} done — {total} candidate(s) queued for entry")
    return {
        "interval": interval,
        "candidates_by_tab": candidates_by_tab,
        "tab_counts": tab_counts,
    }


def _candidate_quote_volume_rank(sym: str) -> int:
    """Lower index = higher 24h quote volume (SCAN_SYMBOLS is volume-sorted)."""
    c = _core()
    sym_u = str(sym or "").strip().upper()
    try:
        return c.SCAN_SYMBOLS.index(sym_u)
    except ValueError:
        return len(c.SCAN_SYMBOLS)


def _sort_entry_candidates(tab_name: str, candidates: list[dict]) -> None:
    """Prioritize top-volume symbols before staggered entry (Tab17 keeps momentum order)."""
    premium_sort_entry_candidates(tab_name, candidates)
    if tab_name == "Tab17":
        return
    candidates.sort(key=lambda item: _candidate_quote_volume_rank(item.get("sym") or ""))


async def execute_scanned_entries(payload: dict | None):
    c = _core()
    """Place live/paper entries for candidates collected by scan_candle_signals."""
    if not payload:
        return
    interval = payload["interval"]
    if not _entry_gate_open(interval):
        print(f"[Entry Gate] Skipping {interval} entries — gate not open yet ({_entry_gate_status(interval)})")
        return

    print(f"Entering {interval} signals (stagger {c.ENTRY_STAGGER_SEC:g}s between attempts)...")
    candidates_by_tab = payload["candidates_by_tab"]
    tab_counts = dict(payload["tab_counts"])
    for tab_name in c.TABS:
        if c.TAB_TIMEFRAMES.get(tab_name) != interval:
            continue
        candidates = list(candidates_by_tab.get(tab_name, []))
        _sort_entry_candidates(tab_name, candidates)
        await _open_balanced_candidates(tab_name, candidates, tab_counts)

    if len(c.state["used_setups"]) > USED_SETUPS_CAP:
        c.state["used_setups"] = c.state["used_setups"][-USED_SETUPS_CAP:]


async def evaluate_candle_signals(interval):
    c = _core()
    """Scan then enter immediately with stagger (manual / legacy path)."""
    payload = await scan_candle_signals(interval)
    if payload is None:
        return
    _reset_entry_stagger()
    await execute_scanned_entries(payload)


async def execute_entry(
    sym,
    sig,
    tab_name,
    *,
    setup_key: str | None = None,
    skip_price_wait: bool = False,
):
    c = _core()
    async with c._state_lock:
        return await _execute_entry_unsafe(
            sym, sig, tab_name, setup_key=setup_key, skip_price_wait=skip_price_wait,
        )

async def _execute_entry_unsafe(
    sym,
    sig,
    tab_name,
    *,
    setup_key: str | None = None,
    skip_price_wait: bool = False,
):
    c = _core()
    if not sig["ep"]:
        print(f"[WARN] execute_entry: ep=0 for {sym} {tab_name}, skipping")
        return
    pos_key = f"{sym}_{tab_name}"
    if pos_key in c.state["open_positions"]:
        return
    if pos_key in _pending_entries_dict():
        return

    side_ok, side_reason = _trade_side_allowed(sig.get("side"), tab_name)
    if not side_ok:
        print(f"[Side Filter] Skip {sym} {tab_name} {sig.get('side')}: {side_reason}")
        return

    sym_ok, sym_reason = c._symbol_entry_allowed(tab_name, sym)
    if not sym_ok:
        print(f"[Symbol Filter] Skip {sym} {tab_name}: {sym_reason}")
        return

    bal_ok, bal_reason = _entry_long_short_balance_allowed(tab_name, sig.get("side"))
    if not bal_ok:
        print(f"[Balance] Skip {sym} {tab_name} {sig.get('side')}: {bal_reason}")
        return

    size_ok, size_reason = _entry_size_allowed(sym, float(sig["ep"]))
    if not size_ok:
        print(f"[Filter] Skip {sym} {tab_name}: {size_reason}")
        return

    cap_blocked, cap_reason = c._notional_cap_blocked()
    if cap_blocked:
        print(f"[Filter] Skip {sym} {tab_name}: {cap_reason}")
        return

    ok, reason = await c._check_entry_quality(sym, sig["side"])
    if not ok:
        print(f"[Filter] Skip {sym} {tab_name}: {reason}")
        return

    if not skip_price_wait and c.ENTRY_ORDER_STYLE != "limit":
        if await _defer_entry_until_better_price(sym, sig, tab_name, setup_key):
            return "price_wait"

    if c.LIVE_MODE:
        # Circuit breaker
        if c._circuit_breaker:
            print(f"[Circuit Breaker] ACTIVE — skipping {sym} {tab_name}")
            return
        if c._binance_rate_limited():
            print(f"[Live] Skip {sym} {tab_name}: Binance rate limit active")
            return
        # Safety: max positions per tab (open + pending limit entries)
        if _tab_slots_remaining(tab_name) <= 0:
            print(f"[Live] Skip {sym} {tab_name}: max positions ({_tab_max_positions(tab_name)}) reached")
            return
        sig_pos_side = c._position_side_name(sig["side"])
        # Margin check: need enough for this order and keep a live-entry safety floor.
        required_margin = max(c._effective_margin_size() * 1.1, c.MIN_ENTRY_AVAILABLE_MARGIN)
        avail_margin    = c.exchange_account.get("availableBalance", 0)
        if avail_margin < required_margin:
            print(f"[Live] Skip {sym}: insufficient margin "
                  f"(avail={avail_margin:.2f} < required={required_margin:.2f})")
            return
        sltp_mode = c._effective_sltp_mode()
        algo_count = await c._open_algo_order_count() if sltp_mode != "local" else None
        prot_plan = c._resolve_entry_protection_plan(sltp_mode, algo_count)
        if prot_plan is None:
            need = 2 if sltp_mode == "binance" else 1
            await c._has_algo_capacity(need, f"{sym} {tab_name} entry")
            return
        sl_local, tp_local, prot_reason = prot_plan
        use_local_protection = sl_local and tp_local
        if use_local_protection:
            print(f"[Live] Local SL/TP: {sym} {tab_name} — bot-managed ({prot_reason})")
        elif sl_local != tp_local:
            print(f"[Live] Hybrid SL/TP: {sym} {tab_name} — SL={'local' if sl_local else 'exchange'} TP={'local' if tp_local else 'exchange'}")
        else:
            print(f"[Live] Binance SL/TP: {sym} {tab_name} — exchange algo orders")
        local_protection_reason = prot_reason if (sl_local or tp_local) else None
        entry_placed = False
        actual_qty = 0.0
        close_side = "SELL" if sig["side"] == "Long" else "BUY"
        pos_side = sig_pos_side
        try:
            await binance_live.set_margin_type(c._http_client, sym, "ISOLATED")
            entry_leverage = await c._ensure_symbol_leverage(sym)

            qty = binance_live.round_qty(sym, c._effective_notional_size() / sig["ep"])
            sim_ok, sim_reason, _planned_sl, _planned_tp, _planned_entry_ref = await c._simulate_entry_protection(
                sym,
                sig,
                qty,
                use_local_protection,
            )
            if not sim_ok:
                msg = f"Skip {sym} {tab_name}: pre-entry protection check failed: {sim_reason}"
                print(f"[Preflight] {msg}")
                await c.record_error_event(msg, severity="warning", source="entry_preflight", notify=False)
                return

            guard_ok, guard_reason = await c._pre_entry_mark_protection_guard(sym, sig)
            if not guard_ok:
                print(f"[Entry Guard] Skip {sym} {tab_name}: {guard_reason}")
                return

            entry_side = "BUY" if sig["side"] == "Long" else "SELL"
            entry_client_id = c._strategy_client_id(tab_name, pos_side, "ENTRY")
            sl_client_id = c._strategy_client_id(tab_name, pos_side, "SL")
            tp_client_id = c._strategy_client_id(tab_name, pos_side, "TP")

            if c.ENTRY_ORDER_STYLE == "limit":
                return await _place_limit_entry_unsafe(
                    sym, sig, tab_name, pos_key=pos_key, pos_side=pos_side,
                    entry_side=entry_side, close_side=close_side, qty=qty,
                    entry_client_id=entry_client_id, setup_key=setup_key,
                    sl_local=sl_local, tp_local=tp_local, prot_reason=prot_reason,
                    use_local_protection=use_local_protection,
                    local_protection_reason=local_protection_reason,
                )

            # 1. Market entry
            entry_res = await binance_live.place_market_order(
                c._http_client, sym, entry_side, qty, position_side=pos_side,
                client_order_id=entry_client_id,
            )
            entry_placed = True
            entry_oid = int(entry_res["orderId"])

            fill_price, filled_qty, fill_source = await c._resolve_live_entry_fill(
                sym, entry_res, entry_client_id=entry_client_id,
            )
            if fill_price <= 0 or filled_qty <= 0:
                msg = f"{sym} {tab_name}: could not resolve live entry fill (no avgPrice/userTrades)"
                print(f"[Entry Guard] {msg} — rolling back")
                await c.record_error_event(msg, severity="warning", source="entry_fill", notify=False)
                try:
                    await binance_live.place_market_order(
                        c._http_client, sym, close_side, qty, position_side=pos_side,
                        client_order_id=c._strategy_client_id(tab_name, pos_side, "CLOSE"),
                    )
                    print(f"[Live] Rollback OK: {sym} closed (unresolved fill)")
                except Exception as rb_err:
                    print(f"[Live] Rollback FAILED {sym} after unresolved fill: {rb_err}")
                entry_placed = False
                return

            actual_qty = binance_live.round_qty(sym, filled_qty)
            if actual_qty <= 0:
                actual_qty = binance_live.round_qty(sym, qty)

            trigger_px = await c._fetch_sltp_trigger_price(sym) or fill_price
            _, trigger_note = c._mark_ref_for_exchange_nudge(fill_price, trigger_px)
            if trigger_note and (not sl_local or not tp_local):
                print(
                    f"[Live] {sym} {tab_name}: protection trigger ref → entry "
                    f"(fill={c._log_price(sym, fill_price)} trigger={c._log_price(sym, trigger_px)} reason={trigger_note})"
                )
            signal_entry = float(sig["ep"])
            sl_price, tp_price = c._planned_protection_prices(
                sym,
                sig["side"],
                fill_price,
                trigger_px,
                float(sig["sl"]),
                float(sig["tp"]),
                signal_entry,
                use_local_protection=use_local_protection,
            )
            sl_diff_pct, tp_diff_pct = c._sltp_diff_pct_from_entry(
                sym,
                sig["side"],
                fill_price,
                sl_price,
                tp_price,
                float(sig["sl"]),
                float(sig["tp"]),
                signal_entry,
            )
            protect_ok, protect_reason = c._validate_planned_protection(
                sym,
                sig["side"],
                actual_qty,
                fill_price,
                sl_price,
                tp_price,
                trigger_px,
                use_local_protection=use_local_protection,
            )
            entry_time_iso = datetime.now().isoformat()
            import time as _time_mod
            grace_until = (
                _time_mod.monotonic() + c.ENTRY_LOCAL_SL_GRACE_SEC
                if c.ENTRY_LOCAL_SL_GRACE_SEC > 0 and (sl_local or tp_local)
                else 0.0
            )

            c.state["open_positions"][pos_key] = {
                "tab": tab_name, "symbol": sym, "side": sig["side"],
                "position_side": pos_side,
                "entry_price": fill_price, "signal_entry_price": signal_entry,
                "signal_sl": float(sig["sl"]), "signal_tp": float(sig["tp"]),
                "placed_sl": sl_price, "placed_tp": tp_price,
                "sl": sl_price, "tp": tp_price,
                "sl_diff_pct": sl_diff_pct, "tp_diff_pct": tp_diff_pct,
                "qty": actual_qty, "entry_time": entry_time_iso,
                "entry_order_id": entry_oid,
                "sl_order_id": None,
                "tp_order_id": None,
                "entry_client_order_id": entry_client_id,
                "sl_client_algo_id": None,
                "tp_client_algo_id": None,
                "protection_status": "entry_saved_pending_protection",
                "local_monitor_after_mono": grace_until,
            }
            c._upsert_position_registry(pos_key, c.state["open_positions"][pos_key], status="entry_pending")
            await c.save_state()

            # ── FIX: initialize order ids before try so rollback except can reference them ──
            sl_oid = None
            tp_oid = None
            try:
                if not protect_ok:
                    raise RuntimeError(f"post-fill protection validation failed: {protect_reason}")
                if not sl_local:
                    sl_res = await binance_live.place_stop_loss(
                        c._http_client, sym, close_side, sl_price, actual_qty, position_side=pos_side,
                        client_algo_id=sl_client_id,
                    )
                    sl_oid = c._algo_id(sl_res)
                if not tp_local:
                    tp_res = await binance_live.place_exchange_take_profit(
                        c._http_client, sym, close_side, tp_price, actual_qty, position_side=pos_side,
                        tp_style=c.SLTP_TP_STYLE, client_algo_id=tp_client_id,
                    )
                    tp_oid = c._algo_id(tp_res)
            except Exception as e:
                body = c._http_error_text(e)
                print(
                    f"[ENTRY | SL/TP FAIL] {c._utc_log_stamp()}  {tab_name}  {sym}  {sig['side']}\n"
                    f"  planned SL {c._log_price(sym, sl_price)}  TP {c._log_price(sym, tp_price)}  qty {actual_qty:g}\n"
                    f"  signal SL {c._log_price(sym, float(sig['sl']))}  TP {c._log_price(sym, float(sig['tp']))}\n"
                    f"  {e} | Binance: {body}"
                )
                await c.record_error_event(
                    f"SL/TP failed for {sym} {tab_name}: {body or e}",
                    severity="critical",
                    source="order_protection",
                    notify=False,
                )
                await c.send_telegram(
                    f"⚠️ <b>ตั้งคำสั่งป้องกันไม่สำเร็จ</b>\n"
                    f"🧠 Strategy: <b>{tab_name} — {c._strategy_label(tab_name)}</b>\n"
                    f"📌 Symbol: <b>{sym}</b> | Side: <b>{sig['side']}</b>\n"
                    f"🛡️ SL: <code>{sig['sl']:.4f}</code> | 🎯 TP: <code>{sig['tp']:.4f}</code>\n"
                    f"🔍 Error: <code>{body or e}</code>",
                    is_error=True,
                )
                # Cancel any partially-placed algo orders for this side first
                if sl_oid:
                    try:
                        await binance_live.cancel_algo_order(c._http_client, algo_id=sl_oid)
                    except Exception:
                        pass
                try:
                    await binance_live.place_market_order(
                        c._http_client, sym, close_side, actual_qty, position_side=pos_side,
                        client_order_id=c._strategy_client_id(tab_name, pos_side, "CLOSE"),
                    )
                    print(f"[Live] Rollback OK: {sym} closed")
                    c.state["open_positions"].pop(pos_key, None)
                    c._mark_position_registry_closed(pos_key, "rollback_after_protection_failure")
                    await c.save_state()
                except Exception as rb_err:
                    await c.send_telegram(
                        f"🚨 <b>ROLLBACK FAILED</b>\n"
                        f"🧠 Strategy: <b>{tab_name} — {c._strategy_label(tab_name)}</b>\n"
                        f"📌 Symbol: <b>{sym}</b> | Side: <b>{sig['side']}</b> | Qty: <code>{actual_qty}</code>\n"
                        f"⚠️ SL/TP error: <code>{e}</code>\n"
                        f"❗ Rollback error: <code>{rb_err}</code>\n"
                        f"<b>MANUAL CLOSE REQUIRED</b>",
                        is_error=True,
                    )
                    await c.record_error_event(
                        f"Rollback failed after SL/TP failure for {sym} {tab_name}: {rb_err}",
                        severity="critical",
                        source="order_protection",
                        notify=False,
                    )
                    print(f"[Live] ROLLBACK FAILED {sym}: {rb_err} - MANUAL CLOSE REQUIRED")
                    pos_record = c.state["open_positions"].get(pos_key)
                    if pos_record:
                        pos_record.update({
                            "sl_order_id": sl_oid,
                            "tp_order_id": tp_oid,
                            "sl_client_algo_id": sl_client_id if sl_oid else None,
                            "tp_client_algo_id": tp_client_id if tp_oid else None,
                            "protection_status": "protection_failed",
                            "protection_mode": "failed",
                            "protection_reason": "sl_tp_failure",
                            "protection_error": str(body or e),
                            "rollback_error": str(rb_err),
                        })
                        c._upsert_position_registry(pos_key, pos_record, status="unprotected")
                        await c.save_state()
                        await c._alert_position_protection_risk(
                            pos_key,
                            pos_record,
                            source="order_protection",
                            notify=True,
                            sync_issue=True,
                        )
                return
            if sl_local:
                sl_oid = None
            if tp_local:
                tp_oid = None

            pos_record = c.state["open_positions"].setdefault(pos_key, {})
            pos_record.update({
                "tab": tab_name, "symbol": sym, "side": sig["side"],
                "position_side": pos_side,
                "leverage": entry_leverage,
                "entry_price": fill_price, "signal_entry_price": signal_entry,
                "signal_sl": float(sig["sl"]), "signal_tp": float(sig["tp"]),
                "placed_sl": sl_price, "placed_tp": tp_price,
                "sl": sl_price, "tp": tp_price,
                "sl_diff_pct": sl_diff_pct, "tp_diff_pct": tp_diff_pct,
                "qty": actual_qty, "entry_time": pos_record.get("entry_time") or entry_time_iso,
                "entry_order_id": entry_oid,
                "sl_order_id":    sl_oid,
                "tp_order_id":    tp_oid,
                "entry_client_order_id": entry_client_id,
                "sl_client_algo_id": None if sl_local else sl_client_id,
                "tp_client_algo_id": None if tp_local else tp_client_id,
                "local_monitor_after_mono": grace_until,
            })
            c._apply_protection_sources(
                pos_record,
                sl_local=sl_local,
                tp_local=tp_local,
                reason=local_protection_reason or prot_reason,
            )
            if not sl_local and not tp_local:
                pos_record.pop("protection_error", None)
                pos_record.pop("rollback_error", None)
            c._upsert_position_registry(pos_key, pos_record, status="open")
            await c.save_state()

            verify_ok, verify_msg = await c._verify_live_position_protection(pos_key)
            pos_record = c.state["open_positions"].get(pos_key)
            if pos_record:
                pos_record["entry_verified_at"] = c._utc_now_iso()
                pos_record["entry_verify_status"] = "ok" if verify_ok else "warning"
                pos_record["entry_verify_message"] = verify_msg
                if not verify_ok and pos_record.get("protection_status") == "exchange":
                    pos_record["protection_status"] = "verify_warning"
                c._upsert_position_registry(pos_key, pos_record, status="open" if verify_ok else "verify_warning")
                await c.save_state()
            if not verify_ok:
                await c.record_error_event(
                    verify_msg,
                    severity="critical",
                    source="entry_verify",
                    notify=True,
                )
                await c.record_sync_issue(verify_msg)
                asyncio.create_task(c.sync_live_positions())
            elif pos_record and c._position_needs_local_exit_monitor(pos_record):
                await c._alert_position_protection_risk(
                    pos_key,
                    pos_record,
                    source="local_protection",
                    notify=True,
                    sync_issue=False,
                )

            await c.save_state()
            c._log_entry_open(
                mode="LIVE",
                tab=tab_name,
                sym=sym,
                side=sig["side"],
                pos_side=pos_side,
                entry_time_iso=pos_record.get("entry_time") or entry_time_iso,
                fill_price=fill_price,
                qty=actual_qty,
                placed_sl=sl_price,
                placed_tp=tp_price,
                signal_ep=signal_entry,
                signal_sl=float(sig["sl"]),
                signal_tp=float(sig["tp"]),
                protection="local" if use_local_protection else ("hybrid" if (sl_local != tp_local) else "exchange"),
                leverage=entry_leverage,
                notional_usd=fill_price * actual_qty,
                fill_source=fill_source or None,
            )
        except Exception as e:
            body = c._http_error_text(e)
            c._note_binance_rate_limit(e)
            detail = f"{e} | Binance: {body}" if body else str(e)
            print(f"[LIVE ENTRY ERROR] {sym} {tab_name}: {detail}")
            if entry_placed and pos_key in c.state.get("open_positions", {}):
                await c._cleanup_failed_live_entry(
                    pos_key, sym, pos_side, close_side, actual_qty, tab_name
                )
            if c._is_binance_cooling_off_error(e, body):
                return "cooling_off"
        return

    # ── Paper trading (simulated exchange execution) ───────────────────────────
    if c._circuit_breaker:
        print(f"[Circuit Breaker] ACTIVE — skipping {sym} {tab_name}")
        return
    tab_pos_count = _tab_open_slot_count(tab_name)
    tab_cap = _tab_max_positions(tab_name)
    if tab_pos_count >= tab_cap:
        print(f"[Paper] Skip {sym} {tab_name}: max positions ({tab_cap}) reached")
        return
    required_margin = max(c._effective_margin_size() * 1.1, c.MIN_ENTRY_AVAILABLE_MARGIN)
    avail_margin = c._paper_available_margin()
    if avail_margin < required_margin:
        print(f"[Paper] Skip {sym}: insufficient virtual margin "
              f"(avail={avail_margin:.2f} < required={required_margin:.2f})")
        return

    mark = await c._fetch_mark_price(sym)
    if mark is None or mark <= 0:
        print(f"[Paper] Skip {sym} {tab_name}: no mark price for entry")
        return
    size_ok, size_reason = _entry_size_allowed(sym, mark)
    if not size_ok:
        print(f"[Paper] Skip {sym} {tab_name}: {size_reason}")
        return

    signal_entry = float(sig["ep"])
    entry_price = await c._paper_simulate_entry_fill(sym, sig["side"], mark)
    qty = binance_live.round_qty(sym, c._effective_notional_size() / entry_price)
    if qty <= 0:
        print(f"[Paper] Skip {sym} {tab_name}: rounded qty is zero")
        return

    sl_price, tp_price = c._protection_prices_from_entry(
        sym,
        sig["side"],
        entry_price,
        float(sig["sl"]),
        float(sig["tp"]),
        signal_entry,
    )
    sl_diff_pct, tp_diff_pct = c._sltp_diff_pct_from_entry(
        sym,
        sig["side"],
        entry_price,
        sl_price,
        tp_price,
        float(sig["sl"]),
        float(sig["tp"]),
        signal_entry,
    )
    protect_ok, protect_reason = c._validate_planned_protection(
        sym,
        sig["side"],
        qty,
        entry_price,
        sl_price,
        tp_price,
        mark,
        use_local_protection=True,
    )
    if not protect_ok:
        print(f"[Paper] Skip {sym} {tab_name}: protection invalid: {protect_reason}")
        return

    trigger_px = c._price_for_sltp(sym) or mark
    guard_ok, guard_reason = c._mark_within_entry_protection(
        sym, sig["side"], trigger_px, sl_price, tp_price,
    )
    if not guard_ok:
        print(f"[Paper] Skip {sym} {tab_name}: {guard_reason}")
        return

    ref_mid = mark
    try:
        book = await c._price_feed_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
        bid = float(book.get("bidPrice") or 0)
        ask = float(book.get("askPrice") or 0)
        if bid > 0 and ask > 0:
            ref_mid = (bid + ask) / 2
    except Exception:
        pass
    entry_slippage_usd = abs(entry_price - ref_mid) * qty
    signal_drift_usd = abs(mark - signal_entry) * qty
    c.state["open_positions"][pos_key] = {
        "tab": tab_name, "symbol": sym, "side": sig["side"],
        "position_side": c._position_side_name(sig["side"]),
        "entry_price": entry_price, "signal_entry_price": signal_entry,
        "mark_entry_price": mark, "ref_mid_entry_price": ref_mid,
        "placed_sl": sl_price, "placed_tp": tp_price,
        "sl": sl_price, "tp": tp_price,
        "signal_sl": sig["sl"], "signal_tp": sig["tp"],
        "sl_diff_pct": sl_diff_pct, "tp_diff_pct": tp_diff_pct,
        "qty": qty,
        "entry_time": datetime.now().isoformat(),
        "entry_slippage_usd": entry_slippage_usd,
        "signal_drift_usd": signal_drift_usd,
        "protection_mode": "paper_mark",
    }
    c._upsert_position_registry(pos_key, c.state["open_positions"][pos_key], status="paper_open")
    await c.save_state()
    entry_iso = c.state["open_positions"][pos_key]["entry_time"]
    c._log_entry_open(
        mode="PAPER",
        tab=tab_name,
        sym=sym,
        side=sig["side"],
        pos_side=c._position_side_name(sig["side"]),
        entry_time_iso=entry_iso,
        fill_price=entry_price,
        qty=qty,
        placed_sl=sl_price,
        placed_tp=tp_price,
        signal_ep=signal_entry,
        signal_sl=float(sig["sl"]),
        signal_tp=float(sig["tp"]),
        protection="paper",
        notional_usd=entry_price * qty,
        fill_source="simulated",
    )


async def _cancel_pending_entry_order(pending: dict) -> None:
    c = _core()
    sym = pending.get("symbol")
    oid = pending.get("entry_order_id")
    if not c.LIVE_MODE or not sym or not oid or not c._http_client:
        return
    try:
        await binance_live.cancel_order(c._http_client, sym, int(oid))
    except Exception as e:
        print(f"[Limit Entry] Cancel {sym} order {oid}: {e}")


def _remove_pending_entry(pos_key: str, *, release_setup: bool = False) -> None:
    c = _core()
    pending = _pending_entries_dict().pop(pos_key, None)
    if release_setup and pending:
        setup_key = pending.get("setup_key")
        if setup_key and setup_key in c.state.get("used_setups", []):
            c.state["used_setups"] = [k for k in c.state["used_setups"] if k != setup_key]


async def _trim_pending_entries_for_tab(tab_name: str) -> int:
    """Cancel oldest pending limit entries when tab exceeds max_positions cap."""
    c = _core()
    cap = _tab_max_positions(tab_name)
    pending_items = [
        (pk, p) for pk, p in _pending_entries_dict().items()
        if p.get("tab") == tab_name
    ]
    excess = _tab_open_slot_count(tab_name) - cap
    if excess <= 0 or not pending_items:
        return 0
    pending_items.sort(key=lambda item: item[1].get("placed_at") or "")
    cancelled = 0
    for pk, p in pending_items[:excess]:
        await _cancel_pending_entry_order(p)
        _remove_pending_entry(pk, release_setup=True)
        cancelled += 1
        print(f"[Limit Entry] Trimmed pending {pk} (max={cap})")
    if cancelled:
        await c.save_state()
    return cancelled


async def _place_limit_entry_unsafe(
    sym: str,
    sig: dict,
    tab_name: str,
    *,
    pos_key: str,
    pos_side: str,
    entry_side: str,
    close_side: str,
    qty: float,
    entry_client_id: str,
    setup_key: str | None,
    sl_local: bool,
    tp_local: bool,
    prot_reason: str,
    use_local_protection: bool,
    local_protection_reason: str | None,
) -> str | None:
    c = _core()
    limit_price = float(sig["ep"])
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(seconds=c.ENTRY_LIMIT_MAX_AGE_SEC)).isoformat()
    try:
        entry_res = await binance_live.place_limit_entry(
            c._http_client, sym, entry_side, limit_price, qty,
            position_side=pos_side, time_in_force=c.ENTRY_LIMIT_TIF,
            client_order_id=entry_client_id,
        )
        entry_oid = int(entry_res.get("orderId") or 0)
        if entry_oid <= 0:
            print(f"[Limit Entry] {sym} {tab_name}: no orderId in response")
            return None
        _pending_entries_dict()[pos_key] = {
            "tab": tab_name,
            "symbol": sym,
            "side": sig["side"],
            "position_side": pos_side,
            "setup_key": setup_key,
            "signal_ts_ms": _setup_signal_ts_ms(setup_key) if setup_key else None,
            "signal_ep": limit_price,
            "sig": dict(sig),
            "qty": qty,
            "filled_qty": 0.0,
            "entry_order_id": entry_oid,
            "entry_client_order_id": entry_client_id,
            "placed_at": now.isoformat(),
            "expires_at": expires_at,
            "sl_local": sl_local,
            "tp_local": tp_local,
            "prot_reason": prot_reason,
            "use_local_protection": use_local_protection,
            "local_protection_reason": local_protection_reason,
        }
        if setup_key and setup_key not in c.state.get("used_setups", []):
            c.state["used_setups"].append(setup_key)
        await c.save_state()
        print(
            f"[Limit Entry] {tab_name} {sym} {sig['side']} @ {c._log_price(sym, limit_price)} "
            f"qty={qty:g} orderId={entry_oid}"
        )
        return "limit_placed"
    except Exception as e:
        body = c._http_error_text(e)
        c._note_binance_rate_limit(e)
        print(f"[Limit Entry] Failed {sym} {tab_name}: {e} | Binance: {body}")
        if c._is_binance_cooling_off_error(e, body):
            return "cooling_off"
        return None


async def complete_limit_entry_fill(
    pos_key: str,
    fill_price: float,
    fill_qty: float,
    *,
    entry_order_id: int | None = None,
) -> None:
    """Promote pending limit entry to open position and place SL/TP."""
    c = _core()
    pending = _pending_entries_dict().get(pos_key)
    if not pending:
        return
    if pos_key in c.state.get("open_positions", {}):
        _remove_pending_entry(pos_key)
        await c.save_state()
        return

    sym = pending["symbol"]
    tab_name = pending["tab"]
    sig = pending.get("sig") or {}
    pos_side = pending.get("position_side") or c._position_side_name(sig.get("side"))
    close_side = "SELL" if sig.get("side") == "Long" else "BUY"
    sl_local = bool(pending.get("sl_local"))
    tp_local = bool(pending.get("tp_local"))
    prot_reason = pending.get("prot_reason") or ""
    use_local_protection = bool(pending.get("use_local_protection"))
    local_protection_reason = pending.get("local_protection_reason")

    actual_qty = binance_live.round_qty(sym, fill_qty)
    if actual_qty <= 0:
        actual_qty = binance_live.round_qty(sym, float(pending.get("qty") or 0))
    if actual_qty <= 0 or fill_price <= 0:
        print(f"[Limit Entry] Skip promote {pos_key}: invalid fill qty/price")
        return

    entry_client_id = pending.get("entry_client_order_id")
    sl_client_id = c._strategy_client_id(tab_name, pos_side, "SL")
    tp_client_id = c._strategy_client_id(tab_name, pos_side, "TP")
    entry_oid = entry_order_id or pending.get("entry_order_id")

    trigger_px = await c._fetch_sltp_trigger_price(sym) or fill_price
    signal_entry = float(pending.get("signal_ep") or sig.get("ep") or fill_price)
    sl_price, tp_price = c._planned_protection_prices(
        sym, sig["side"], fill_price, trigger_px,
        float(sig["sl"]), float(sig["tp"]), signal_entry,
        use_local_protection=use_local_protection,
    )
    sl_diff_pct, tp_diff_pct = c._sltp_diff_pct_from_entry(
        sym, sig["side"], fill_price, sl_price, tp_price,
        float(sig["sl"]), float(sig["tp"]), signal_entry,
    )
    protect_ok, protect_reason = c._validate_planned_protection(
        sym, sig["side"], actual_qty, fill_price, sl_price, tp_price, trigger_px,
        use_local_protection=use_local_protection,
    )
    entry_time_iso = datetime.now(timezone.utc).isoformat()
    import time as _time_mod
    grace_until = (
        _time_mod.monotonic() + c.ENTRY_LOCAL_SL_GRACE_SEC
        if c.ENTRY_LOCAL_SL_GRACE_SEC > 0 and (sl_local or tp_local)
        else 0.0
    )

    c.state["open_positions"][pos_key] = {
        "tab": tab_name, "symbol": sym, "side": sig["side"],
        "position_side": pos_side,
        "entry_price": fill_price, "signal_entry_price": signal_entry,
        "signal_sl": float(sig["sl"]), "signal_tp": float(sig["tp"]),
        "placed_sl": sl_price, "placed_tp": tp_price,
        "sl": sl_price, "tp": tp_price,
        "sl_diff_pct": sl_diff_pct, "tp_diff_pct": tp_diff_pct,
        "qty": actual_qty, "entry_time": entry_time_iso,
        "entry_order_id": entry_oid,
        "sl_order_id": None, "tp_order_id": None,
        "entry_client_order_id": entry_client_id,
        "sl_client_algo_id": None, "tp_client_algo_id": None,
        "protection_status": "entry_saved_pending_protection",
        "local_monitor_after_mono": grace_until,
    }
    c._upsert_position_registry(pos_key, c.state["open_positions"][pos_key], status="entry_pending")
    _remove_pending_entry(pos_key)
    await c.save_state()

    sl_oid = tp_oid = None
    try:
        if not protect_ok:
            raise RuntimeError(f"post-fill protection validation failed: {protect_reason}")
        if not sl_local:
            sl_res = await binance_live.place_stop_loss(
                c._http_client, sym, close_side, sl_price, actual_qty, position_side=pos_side,
                client_algo_id=sl_client_id,
            )
            sl_oid = c._algo_id(sl_res)
        if not tp_local:
            tp_res = await binance_live.place_exchange_take_profit(
                c._http_client, sym, close_side, tp_price, actual_qty, position_side=pos_side,
                tp_style=c.SLTP_TP_STYLE, client_algo_id=tp_client_id,
            )
            tp_oid = c._algo_id(tp_res)
    except Exception as e:
        body = c._http_error_text(e)
        print(f"[Limit Entry] SL/TP fail after fill {pos_key}: {e} | {body}")
        await c.record_error_event(
            f"SL/TP failed after limit fill for {sym} {tab_name}: {body or e}",
            severity="critical", source="order_protection", notify=False,
        )
        if sl_oid:
            try:
                await binance_live.cancel_algo_order(c._http_client, algo_id=sl_oid)
            except Exception:
                pass
        try:
            await binance_live.place_market_order(
                c._http_client, sym, close_side, actual_qty, position_side=pos_side,
                client_order_id=c._strategy_client_id(tab_name, pos_side, "CLOSE"),
            )
            c.state["open_positions"].pop(pos_key, None)
            c._mark_position_registry_closed(pos_key, "rollback_after_limit_protection_failure")
            await c.save_state()
        except Exception as rb_err:
            print(f"[Limit Entry] Rollback failed {pos_key}: {rb_err}")
            pos_record = c.state["open_positions"].get(pos_key)
            if pos_record:
                pos_record.update({
                    "sl_order_id": sl_oid,
                    "tp_order_id": tp_oid,
                    "sl_client_algo_id": sl_client_id if sl_oid else None,
                    "tp_client_algo_id": tp_client_id if tp_oid else None,
                    "protection_status": "protection_failed",
                    "protection_mode": "failed",
                    "protection_reason": "sl_tp_failure",
                    "protection_error": str(body or e),
                    "rollback_error": str(rb_err),
                })
                c._upsert_position_registry(pos_key, pos_record, status="unprotected")
                await c.save_state()
                await c._alert_position_protection_risk(
                    pos_key, pos_record, source="order_protection", notify=True,
                )
        return

    pos_record = c.state["open_positions"].setdefault(pos_key, {})
    entry_leverage = await c._ensure_symbol_leverage(sym)
    pos_record.update({
        "leverage": entry_leverage,
        "sl_order_id": sl_oid, "tp_order_id": tp_oid,
        "sl_client_algo_id": None if sl_local else sl_client_id,
        "tp_client_algo_id": None if tp_local else tp_client_id,
        "local_monitor_after_mono": grace_until,
    })
    c._apply_protection_sources(
        pos_record, sl_local=sl_local, tp_local=tp_local,
        reason=local_protection_reason or prot_reason,
    )
    c._upsert_position_registry(pos_key, pos_record, status="open")
    await c.save_state()
    c._log_entry_open(
        mode="LIVE", tab=tab_name, sym=sym, side=sig["side"], pos_side=pos_side,
        entry_time_iso=entry_time_iso, fill_price=fill_price, qty=actual_qty,
        placed_sl=sl_price, placed_tp=tp_price, signal_ep=signal_entry,
        signal_sl=float(sig["sl"]), signal_tp=float(sig["tp"]),
        protection="local" if use_local_protection else ("hybrid" if (sl_local != tp_local) else "exchange"),
        leverage=entry_leverage, notional_usd=fill_price * actual_qty, fill_source="limit",
    )


async def handle_entry_limit_order_update(order: dict, status: str) -> None:
    """UDS handler for limit entry orders (ENTRY client id role)."""
    c = _core()
    client_id = c._order_client_id(order)
    tab_name = c._strategy_from_client_id(client_id)
    sym = order.get("s")
    if not tab_name or not sym:
        return
    pos_key = f"{sym}_{tab_name}"
    pending = _pending_entries_dict().get(pos_key)
    if not pending:
        return

    status_u = str(status or "").upper()
    fill_price = float(order.get("ap") or order.get("L") or order.get("p") or 0)
    cum_qty = float(order.get("z") or order.get("q") or 0)

    if status_u == "PARTIALLY_FILLED":
        pending["filled_qty"] = cum_qty
        await c.save_state()
        return

    if status_u in ("CANCELED", "EXPIRED", "REJECTED"):
        had_fill = float(pending.get("filled_qty") or 0) > 0
        await _cancel_pending_entry_order(pending)
        _remove_pending_entry(pos_key, release_setup=not had_fill)
        await c.save_state()
        if had_fill:
            print(f"[Limit Entry] {pos_key} cancelled with partial fill {pending.get('filled_qty')} — sync will reconcile")
            from bot.engine.sync import sync_live_positions
            asyncio.create_task(sync_live_positions())
        return

    if status_u != "FILLED":
        return

    await complete_limit_entry_fill(
        pos_key, fill_price, cum_qty,
        entry_order_id=int(order.get("i") or pending.get("entry_order_id") or 0) or None,
    )


async def _process_expired_pending_entries() -> None:
    c = _core()
    if not c.LIVE_MODE:
        return
    now = datetime.now(timezone.utc)
    for pos_key, pending in list(_pending_entries_dict().items()):
        expires_raw = pending.get("expires_at")
        if not expires_raw:
            continue
        try:
            expires_dt = datetime.fromisoformat(str(expires_raw).replace("Z", "+00:00"))
            if expires_dt.tzinfo is None:
                expires_dt = expires_dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if now < expires_dt:
            continue
        filled = float(pending.get("filled_qty") or 0)
        print(f"[Limit Entry] TTL expired {pos_key} filled={filled:g}")
        await _cancel_pending_entry_order(pending)
        if filled > 0:
            sym = pending.get("symbol")
            fill_price = float(pending.get("signal_ep") or 0)
            await complete_limit_entry_fill(pos_key, fill_price, filled)
        else:
            _remove_pending_entry(pos_key, release_setup=True)
        await c.save_state()


async def reconcile_pending_entry_orders() -> None:
    """Startup/sync: align pending_entry_orders with exchange openOrders."""
    c = _core()
    if not c.LIVE_MODE or not c._http_client:
        return
    pending = _pending_entries_dict()
    if not pending:
        return
    symbols = {p.get("symbol") for p in pending.values() if p.get("symbol")}
    live_ids: dict[str, set[int]] = {}
    for sym in symbols:
        try:
            orders = await binance_live.get_open_orders(c._http_client, sym)
            live_ids[sym] = {int(o.get("orderId") or 0) for o in orders if o.get("orderId")}
        except Exception as e:
            print(f"[Limit Entry] reconcile openOrders {sym}: {e}")
            live_ids[sym] = set()

    for pos_key, p in list(pending.items()):
        sym = p.get("symbol")
        oid = int(p.get("entry_order_id") or 0)
        if sym and oid and oid not in live_ids.get(sym, set()):
            print(f"[Limit Entry] Reconcile remove stale pending {pos_key} (order {oid} gone)")
            _remove_pending_entry(pos_key, release_setup=True)

    for tab in c.TABS:
        await _trim_pending_entries_for_tab(tab)
    await c.save_state()


async def entry_limit_ttl_loop():
    c = _core()
    while True:
        try:
            await _process_expired_pending_entries()
        except Exception as e:
            print(f"[Limit Entry] TTL loop error: {e}")
        await asyncio.sleep(15)


# Backward-compatible alias for tests / core re-exports.
_build_tab17_momentum_universe = build_tab17_momentum_universe
