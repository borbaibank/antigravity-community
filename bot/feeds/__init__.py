"""Price / kline / WebSocket feeds."""

from bot.feeds.klines import get_klines
from bot.feeds.market import (
    fetch_scan_symbols,
    _check_entry_quality,
    _fetch_mark_price,
    _fetch_sltp_trigger_price,
    _last_or_mark,
    _mark_or_last,
    _price_for_sltp,
    _price_feed_get,
    _refresh_open_position_marks,
)
from bot.feeds.ws import (
    binance_ws_loop,
    price_poll_loop,
    price_tick_monitor_loop,
)

_LAZY_IN_CORE = frozenset({
    "refresh_scan_symbols_loop",
})


def __getattr__(name: str):
    if name in _LAZY_IN_CORE:
        from bot import core
        return getattr(core, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "binance_ws_loop",
    "fetch_scan_symbols",
    "get_klines",
    "price_poll_loop",
    "price_tick_monitor_loop",
    "refresh_scan_symbols_loop",
    "_check_entry_quality",
    "_fetch_mark_price",
    "_fetch_sltp_trigger_price",
    "_last_or_mark",
    "_mark_or_last",
    "_price_for_sltp",
    "_price_feed_get",
    "_refresh_open_position_marks",
]
