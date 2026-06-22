"""SL/TP price planning from entry fill (extracted from bot.core)."""

from __future__ import annotations

import binance_live
from config import EXCHANGE_MARK_NUDGE_PCT, MAX_SL_PCT

def _core():
    from bot import core
    return core


def _protection_prices_from_entry(
    sym: str,
    side: str,
    entry_px: float,
    raw_sl: float,
    raw_tp: float,
    signal_entry_px: float | None = None,
):
    c = _core()
    """Anchor SL/TP at entry_px using risk/reward distances from the strategy signal.

    Signal distances are measured from planned entry (``ep``) to ``sl``/``tp``;
    the same absolute distances are applied from the actual fill (paper or live).
    """
    signal_entry = float(signal_entry_px or entry_px)
    sl_dist = abs(signal_entry - float(raw_sl))
    tp_dist = abs(float(raw_tp) - signal_entry)
    if sl_dist <= 0 or tp_dist <= 0:
        sl_dist = max(sl_dist, entry_px * 0.005)
        tp_dist = max(tp_dist, sl_dist)
    if side == "Long":
        sl_price = entry_px - sl_dist
        tp_price = entry_px + tp_dist
        sl_price = max(sl_price, entry_px * (1 - c.MAX_SL_PCT))
    else:
        sl_price = entry_px + sl_dist
        tp_price = entry_px - tp_dist
        sl_price = min(sl_price, entry_px * (1 + c.MAX_SL_PCT))
    return (
        c._round_protection_price(sym, sl_price, side=side, kind="sl"),
        c._round_protection_price(sym, tp_price, side=side, kind="tp"),
    )


def _paper_available_margin():
    c = _core()
    """Virtual available margin for paper entries (mirrors live margin gate shape)."""
    tab_total = sum(
        float(v or 0)
        for k, v in c.state.get("balances", {}).items()
        if not str(k).startswith("_") and isinstance(v, (int, float))
    )
    reserved = sum(
        c._effective_margin_size() * 1.1
        for _ in c.state.get("open_positions", {}).values()
    )
    return tab_total - reserved


def _http_error_text(exc: Exception):
    c = _core()
    return getattr(getattr(exc, "response", None), "text", "") or ""


def _is_algo_limit_error(exc: Exception):
    c = _core()
    text = c._http_error_text(exc)
    return "-4045" in text or "Reach max stop order limit" in text


def _is_binance_cooling_off_error(exc: Exception | None = None, body: str = ""):
    c = _core()
    text = body or (c._http_error_text(exc) if exc else "") or str(exc or "")
    return "-4192" in text or "Cooling-off Period" in text or "cooling-off period" in text.lower()


def _is_immediate_trigger_error(exc: Exception | None = None, body: str = ""):
    c = _core()
    text = body or (c._http_error_text(exc) if exc else "") or str(exc or "")
    return "-2021" in text or "immediately trigger" in text.lower()


def _exchange_sl_crossed_mark(is_long: bool, sl_px: float, mark_px: float | None):
    c = _core()
    """True when mark is at/past stored SL (with exchange nudge buffer)."""
    if not mark_px or sl_px <= 0:
        return False
    nudge = c.EXCHANGE_MARK_NUDGE_PCT
    if is_long:
        return sl_px >= mark_px * (1.0 - nudge)
    return sl_px <= mark_px * (1.0 + nudge)


def _exchange_tp_crossed_mark(is_long: bool, tp_px: float, mark_px: float | None):
    c = _core()
    """True when mark is at/past stored TP (with exchange nudge buffer)."""
    if not mark_px or tp_px <= 0:
        return False
    nudge = c.EXCHANGE_MARK_NUDGE_PCT
    if is_long:
        return tp_px <= mark_px * (1.0 + nudge)
    return tp_px >= mark_px * (1.0 - nudge)


def _missing_exchange_protection_reason(pos: dict, price: float | None):
    c = _core()
    """Exchange-managed leg missing from c.state but mark already crossed the level."""
    if not c.LIVE_MODE or price is None:
        return None
    sl_px = float(pos.get("sl", 0) or 0)
    tp_px = float(pos.get("tp", 0) or 0)
    is_long = pos.get("side") == "Long"
    if not c._position_sl_is_local(pos) and not pos.get("sl_order_id") and sl_px > 0:
        if c._exchange_sl_crossed_mark(is_long, sl_px, price):
            return "SL"
    if not c._position_tp_is_local(pos) and not pos.get("tp_order_id") and tp_px > 0:
        if c._exchange_tp_crossed_mark(is_long, tp_px, price):
            return "TP"
    return None


def _round_protection_price(sym: str, price: float, *, side: str | None = None, kind: str | None = None):
    c = _core()
    if sym in binance_live.symbol_info:
        if side and kind:
            sk = str(side).lower()
            kk = str(kind).lower()
            if sk == "long":
                mode = "down" if kk == "sl" else "up"
            else:
                mode = "up" if kk == "sl" else "down"
            return binance_live.round_price_tick(sym, price, mode=mode)
        return binance_live.round_price(sym, price)
    return round(float(price), 8)


def _mark_ref_for_exchange_nudge(entry_px: float, mark_px: float | None):
    c = _core()
    """Pick mark for exchange SL/TP nudge; fall back to entry when mark is missing or untrusted."""
    entry = float(entry_px or 0)
    if entry <= 0:
        m = float(mark_px or 0)
        return (m if m > 0 else entry), None
    mark = float(mark_px or 0)
    if mark <= 0:
        return entry, "mark_missing"
    drift = abs(mark - entry) / entry
    if drift > c.MARK_FILL_SANITY_PCT:
        return entry, f"mark_drift_{drift:.2%}"
    return mark, None


def _clamp_exchange_nudge(
    side: str,
    entry_px: float,
    strat_sl: float,
    strat_tp: float,
    sl_price: float,
    tp_price: float,
):
    c = _core()
    """Cap how far exchange mark nudge may move SL/TP away from strategy-anchored levels."""
    cap = float(entry_px or 0) * c.MAX_EXCHANGE_PROTECTION_NUDGE_PCT
    if cap <= 0:
        return sl_price, tp_price
    if side == "Long":
        tp_price = min(tp_price, strat_tp + cap)
        sl_price = max(sl_price, strat_sl - cap)
    else:
        tp_price = max(tp_price, strat_tp - cap)
        sl_price = min(sl_price, strat_sl + cap)
    return sl_price, tp_price


def _planned_protection_prices(
    sym: str,
    side: str,
    entry_px: float,
    mark_px: float | None,
    raw_sl: float,
    raw_tp: float,
    signal_entry_px: float | None = None,
    *,
    use_local_protection: bool = False,
):
    c = _core()
    """SL/TP from fill + strategy distances; exchange algo path nudges away from trusted mark."""
    sl_price, tp_price = c._protection_prices_from_entry(
        sym, side, entry_px, raw_sl, raw_tp, signal_entry_px
    )
    if use_local_protection:
        return (
            c._round_protection_price(sym, sl_price, side=side, kind="sl"),
            c._round_protection_price(sym, tp_price, side=side, kind="tp"),
        )

    strat_sl, strat_tp = sl_price, tp_price
    mark, _ = c._mark_ref_for_exchange_nudge(entry_px, mark_px)
    nudge = c.EXCHANGE_MARK_NUDGE_PCT
    if side == "Long":
        sl_price = min(sl_price, mark * (1 - nudge))
        sl_price = max(sl_price, entry_px * (1 - c.MAX_SL_PCT))
        tp_price = max(tp_price, mark * (1 + nudge))
    else:
        sl_price = max(sl_price, mark * (1 + nudge))
        sl_price = min(sl_price, entry_px * (1 + c.MAX_SL_PCT))
        tp_price = min(tp_price, mark * (1 - nudge))
    sl_price, tp_price = c._clamp_exchange_nudge(
        side, entry_px, strat_sl, strat_tp, sl_price, tp_price
    )
    return (
        c._round_protection_price(sym, sl_price, side=side, kind="sl"),
        c._round_protection_price(sym, tp_price, side=side, kind="tp"),
    )
