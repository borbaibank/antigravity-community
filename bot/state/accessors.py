"""State accessors — effective/normalize helpers extracted from bot.core."""

from __future__ import annotations

from datetime import datetime, timezone

from config import (
    BINANCE_CLOSE_HISTORY_ENABLED,
    BINANCE_TESTNET,
    INITIAL_BALANCE,
    LIVE_MODE,
    LEVERAGE,
    MARGIN_SIZE,
    MAX_NOTIONAL_SIZE,
    MAX_POSITIONS_PER_TAB,
    NOTIONAL_SIZE,
    SLTP_MODE,
    SLTP_MODES,
    SYMBOL_FILTER_DEFAULT_MIN_NET_PNL,
    SYMBOL_FILTER_DEFAULT_MIN_TRADES,
    SYMBOL_FILTER_DEFAULT_MIN_WIN_RATE,
    SYMBOL_FILTER_MODES,
    SYMBOL_FILTER_ROLLING_WINDOW,
    SYMBOL_SCAN_LIMIT,
    TAB_TIMEFRAMES,
    TABS,
)
from bot.engine.premium_config import tab17_base_universe


def _core():
    from bot import core
    return core


def _match_float_option(value: float, options: tuple[float, ...], default: float):
    c = _core()
    for opt in options:
        if abs(value - opt) < 1e-6:
            return opt
    return default


def _env_sizing() -> dict:
    """Authoritative sizing from config (.env). Not mutable via dashboard."""
    c = _core()
    try:
        lev_raw = int(LEVERAGE)
    except (TypeError, ValueError):
        lev_raw = int(c.LEVERAGE)
    lev = lev_raw if lev_raw in c.LEVERAGE_OPTIONS else int(c.LEVERAGE)
    try:
        notional = float(NOTIONAL_SIZE)
    except (TypeError, ValueError):
        notional = float(c.NOTIONAL_SIZE)
    if notional <= 0:
        notional = float(c.NOTIONAL_SIZE)
    try:
        margin = float(MARGIN_SIZE)
    except (TypeError, ValueError):
        margin = notional / lev if lev > 0 else notional
    if margin <= 0:
        margin = notional / lev if lev > 0 else notional
    return {
        "leverage": lev,
        "notional_size": notional,
        "margin_size": margin,
    }


def _effective_leverage():
    return _env_sizing()["leverage"]


def _effective_margin_size():
    return _env_sizing()["margin_size"]


def _effective_max_positions():
    c = _core()
    raw = c.state.get("max_positions_per_tab", c.MAX_POSITIONS_PER_TAB)
    try:
        v = int(raw)
    except (TypeError, ValueError):
        v = c.MAX_POSITIONS_PER_TAB
    return v if v in c.MAX_POSITIONS_OPTIONS else c.MAX_POSITIONS_PER_TAB


def _effective_long_short_balance_mode(tab: str):
    c = _core()
    return c.state.get("long_short_balance_mode", "off")


def _effective_trade_side_mode(tab: str):
    c = _core()
    return c.state.get("trade_side_mode", "both")


def _effective_notional_size():
    return _env_sizing()["notional_size"]


def _notional_cap_blocked() -> tuple[bool, str]:
    """Return (blocked, reason) when MAX_NOTIONAL_SIZE env cap is exceeded."""
    cap = float(MAX_NOTIONAL_SIZE or 0)
    if cap <= 0:
        return False, ""
    ns = _effective_notional_size()
    if ns > cap + 1e-6:
        return True, f"notional {ns:.2f} > MAX_NOTIONAL_SIZE {cap:.2f}"
    return False, ""


def _sync_sizing_state(
    *,
    anchor: str,
    margin_size: float | None = None,
    leverage: int | None = None,
    notional_size: float | None = None,
):
    c = _core()
    """Keep margin_size, leverage, notional_size in sync (notional = margin × leverage)."""
    lev = int(leverage if leverage is not None else c._effective_leverage())
    if lev not in c.LEVERAGE_OPTIONS:
        lev = c.LEVERAGE

    if anchor == "notional":
        if notional_size is None:
            notional_size = c._effective_notional_size()
        notional = float(notional_size)
        margin = notional / lev if lev > 0 else notional
    elif anchor == "leverage":
        margin = float(margin_size if margin_size is not None else c._effective_margin_size())
        notional = margin * lev
    else:  # margin
        margin = float(margin_size if margin_size is not None else c._effective_margin_size())
        notional = margin * lev

    if margin <= 0:
        margin = c._DEFAULT_MARGIN_SIZE
        notional = margin * lev

    c.state["margin_size"] = margin
    c.state["leverage"] = lev
    c.state["notional_size"] = notional
    return {
        "margin_size": margin,
        "leverage": lev,
        "notional_size": notional,
    }


def _normalize_sizing_state():
    c = _core()
    """Mirror config (.env) sizing into state for dashboard WS payload."""
    sizing = _env_sizing()
    c.state["margin_size"] = sizing["margin_size"]
    c.state["leverage"] = sizing["leverage"]
    c.state["notional_size"] = sizing["notional_size"]


def _effective_symbol_scan_limit():
    c = _core()
    raw = c.state.get("symbol_scan_limit", c.SYMBOL_SCAN_LIMIT)
    try:
        v = int(raw)
    except (TypeError, ValueError):
        v = c.SYMBOL_SCAN_LIMIT
    return v if v in c.SYMBOL_SCAN_OPTIONS else c.SYMBOL_SCAN_LIMIT


def _clamp_symbol_scan_limit(raw, tab: str | None = None):
    c = _core()
    """Map stored scan limit to a valid option (legacy values > max snap down)."""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    if tab == "Tab17":
        cap = tab17_base_universe()
        if cap is not None and v == cap:
            return cap
    if v in c.SYMBOL_SCAN_OPTIONS:
        return v
    max_opt = max(c.SYMBOL_SCAN_OPTIONS)
    if v > max_opt:
        return max_opt
    for opt in reversed(c.SYMBOL_SCAN_OPTIONS):
        if v >= opt:
            return opt
    return None


def _normalize_symbol_scan_limit_by_tab():
    c = _core()
    """Clamp per-tab scan overrides to valid tabs and c.SYMBOL_SCAN_OPTIONS."""
    raw = c.state.get("symbol_scan_limit_by_tab")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for tab, value in raw.items():
        if tab not in c.TABS or tab == "Tab7":
            continue
        v = c._clamp_symbol_scan_limit(value, tab)
        if v is not None:
            out[tab] = v
    return out


def _effective_symbol_scan_limit_for_tab(tab: str):
    c = _core()
    return c._normalize_symbol_scan_limit_by_tab().get(tab, c._effective_symbol_scan_limit())


def _scan_universe_size():
    c = _core()
    """How many ranked symbols to keep loaded (max of global + per-tab overrides)."""
    sizes = [c._effective_symbol_scan_limit(), max(c.SYMBOL_SCAN_OPTIONS)]
    sizes.extend(c._normalize_symbol_scan_limit_by_tab().values())
    cap = tab17_base_universe()
    if cap is not None and "Tab17" in c.TABS:
        sizes.append(cap)
    return max(sizes)


def _interval_has_enabled_tabs(interval: str):
    c = _core()
    return any(
        c.TAB_TIMEFRAMES.get(tab) == interval and c._tab_on(tab)
        for tab in c.TABS
    )


def _symbols_base_universe(interval: str):
    c = _core()
    """Union slice for enabled tabs on this candle interval (ranked by volume)."""
    if not c.SCAN_SYMBOLS:
        return []
    limits: list[int] = []
    for tab in c.TABS:
        if c.TAB_TIMEFRAMES.get(tab) != interval or not c._tab_on(tab):
            continue
        limits.append(c._effective_symbol_scan_limit_for_tab(tab))
    if not limits:
        return []
    return list(c.SCAN_SYMBOLS[:max(limits)])


def _symbols_for_interval_scan(interval: str, hour_slot=None):
    c = _core()
    """Scan universe — prescreen watchlist when saved for this candle slot, else full base."""
    from datetime import datetime, timezone

    from bot.engine.prescreen import active_prescreen_symbols

    base = _symbols_base_universe(interval)
    if not base:
        return []
    if hour_slot is None:
        hour_slot = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    watchlist = active_prescreen_symbols(interval, hour_slot)
    if not watchlist:
        return base
    base_set = set(base)
    filtered = [sym for sym in watchlist if sym in base_set]
    if not filtered:
        return base
    return filtered


def _kline_request_weight(limit: int):
    c = _core()
    """Binance USDT-M futures GET /fapi/v1/klines request weight by limit param."""
    if limit <= 100:
        return 1
    if limit <= 500:
        return 5
    if limit <= 1000:
        return 10
    return 20


_MAINNET_LOCAL_SLTP_REASON = "mainnet_local_sl_tp"
_BINANCE_EXCHANGE_REASON = "binance_exchange"
_HYBRID_SLTP_REASON = "hybrid_sl_ex_tp_local"
_FALLBACK_LOCAL_REASON = "binance_fallback_local"
_LOCAL_SLTP_POLICY_REASONS = frozenset({
    _MAINNET_LOCAL_SLTP_REASON,
    _FALLBACK_LOCAL_REASON,
    _HYBRID_SLTP_REASON,
    "testnet_local_policy",
})


def _normalize_sltp_mode(raw: str | None):
    c = _core()
    mode = str(raw or "").strip().lower()
    return mode if mode in c.SLTP_MODES else c.SLTP_MODE


def _default_sltp_mode_from_state_fields():
    c = _core()
    if "sltp_mode" in c.state:
        return c._normalize_sltp_mode(c.state.get("sltp_mode"))
    if "local_sltp" in c.state:
        return "local" if bool(c.state["local_sltp"]) else "binance"
    return c.SLTP_MODE


def _effective_sltp_mode():
    c = _core()
    """Runtime SL/TP mode: dashboard c.state overrides .env c.SLTP_MODE default."""
    return c._default_sltp_mode_from_state_fields()


def _effective_local_sltp():
    c = _core()
    return c._effective_sltp_mode() == "local"


def _max_open_algo_orders():
    c = _core()
    return 100 if c.BINANCE_TESTNET else 200


def _startup_tab_enabled():
    c = _core()
    return {tab: tab in c.STARTUP_ENABLED_TABS for tab in c.TABS}

def _normalize_tab_enabled(saved: dict | None = None):
    c = _core()
    defaults = c._startup_tab_enabled()
    if not isinstance(saved, dict):
        return defaults
    return {tab: bool(saved.get(tab, defaults[tab])) for tab in c.TABS}


_TAB_STATS_VERSION = 2


def _is_winning_trade(pnl_usd: float):
    c = _core()
    """Win = net PnL strictly positive after fees (breakeven is not a win)."""
    return float(pnl_usd or 0) > 0


def _empty_tab_stats_row():
    c = _core()
    return {"trades": 0, "wins": 0, "best": None, "worst": None}


def _normalize_tab_stats():
    c = _core()
    tabs = c.state.setdefault("tab_stats", {})
    for tab in list(c.TABS) + ["SafeGuard", "Recovered"]:
        if tab not in tabs or not isinstance(tabs.get(tab), dict):
            tabs[tab] = c._empty_tab_stats_row()
        else:
            row = tabs[tab]
            row.setdefault("trades", 0)
            row.setdefault("wins", 0)
            row.setdefault("best", None)
            row.setdefault("worst", None)
    return tabs


def _accumulate_tab_stats_row(row: dict, entry: dict):
    c = _core()
    pnl = float(entry.get("pnl_usd") or 0)
    row["trades"] = int(row.get("trades", 0) or 0) + 1
    if c._is_winning_trade(pnl):
        row["wins"] = int(row.get("wins", 0) or 0) + 1
    best = row.get("best")
    worst = row.get("worst")
    if best is None or pnl > float(best):
        row["best"] = pnl
    if worst is None or pnl < float(worst):
        row["worst"] = pnl


def _rebuild_tab_stats_from_history():
    c = _core()
    tabs = {tab: c._empty_tab_stats_row() for tab in list(c.TABS) + ["SafeGuard", "Recovered"]}
    for entry in c.state.get("history", []):
        tab = entry.get("tab") or "Recovered"
        if tab not in tabs:
            tabs[tab] = c._empty_tab_stats_row()
        c._accumulate_tab_stats_row(tabs[tab], entry)
    c.state["tab_stats"] = tabs
    c.state["tab_stats_version"] = _TAB_STATS_VERSION


def _record_tab_stats_close(tab: str, pnl_usd: float, *, entry: dict | None = None):
    c = _core()
    tab_name = tab or "Recovered"
    row = c._normalize_tab_stats().setdefault(tab_name, c._empty_tab_stats_row())
    c._accumulate_tab_stats_row(row, {"pnl_usd": pnl_usd})
    if entry and entry.get("symbol"):
        c._record_symbol_stats_close(tab_name, str(entry["symbol"]), entry)


_SYMBOL_STATS_VERSION = 1


def _empty_symbol_stats_row():
    c = _core()
    return {
        "trades": 0,
        "wins": 0,
        "net_pnl": 0.0,
        "long_trades": 0,
        "short_trades": 0,
        "long_pnl": 0.0,
        "short_pnl": 0.0,
    }


def _accumulate_symbol_stats_row(row: dict, entry: dict):
    c = _core()
    pnl = float(entry.get("pnl_usd") or 0)
    side = str(entry.get("side") or "")
    row["trades"] = int(row.get("trades", 0) or 0) + 1
    row["net_pnl"] = float(row.get("net_pnl", 0) or 0) + pnl
    if c._is_winning_trade(pnl):
        row["wins"] = int(row.get("wins", 0) or 0) + 1
    if side == "Long":
        row["long_trades"] = int(row.get("long_trades", 0) or 0) + 1
        row["long_pnl"] = float(row.get("long_pnl", 0) or 0) + pnl
    elif side == "Short":
        row["short_trades"] = int(row.get("short_trades", 0) or 0) + 1
        row["short_pnl"] = float(row.get("short_pnl", 0) or 0) + pnl


def _normalize_symbol_stats():
    c = _core()
    tabs = c.state.setdefault("symbol_stats", {})
    if not isinstance(tabs, dict):
        tabs = {}
        c.state["symbol_stats"] = tabs
    return tabs


def _record_symbol_stats_close(tab: str, sym: str, entry: dict):
    c = _core()
    tab_name = tab or "Recovered"
    tab_rows = c._normalize_symbol_stats().setdefault(tab_name, {})
    if not isinstance(tab_rows, dict):
        tab_rows = {}
        c._normalize_symbol_stats()[tab_name] = tab_rows
    row = tab_rows.setdefault(sym, c._empty_symbol_stats_row())
    c._accumulate_symbol_stats_row(row, entry)
    c._invalidate_rolling_symbol_stats_cache()


def _rebuild_symbol_stats_from_history():
    c = _core()
    tabs: dict[str, dict[str, dict]] = {}
    for entry in c.state.get("history", []):
        tab = entry.get("tab") or "Recovered"
        sym = entry.get("symbol")
        if not sym:
            continue
        tabs.setdefault(tab, {})
        sym_row = tabs[tab].setdefault(sym, c._empty_symbol_stats_row())
        c._accumulate_symbol_stats_row(sym_row, entry)
    c.state["symbol_stats"] = tabs
    c.state["symbol_stats_version"] = _SYMBOL_STATS_VERSION
    c._invalidate_rolling_symbol_stats_cache()


_rolling_symbol_stats_cache: dict = {"key": None, "data": None}


def _invalidate_rolling_symbol_stats_cache():
    c = _core()
    _rolling_symbol_stats_cache["key"] = None
    _rolling_symbol_stats_cache["data"] = None


def _rolling_symbol_stats_cache_key(window: int):
    c = _core()
    hist = c.state.get("history") or []
    tail = hist[-1] if hist else None
    tail_sig = None
    if tail:
        tail_sig = (
            tail.get("closed_at"),
            tail.get("exit_ts"),
            tail.get("symbol"),
            tail.get("pnl_usd"),
        )
    return (len(hist), tail_sig, window)


def _all_rolling_symbol_stats(window: int | None = None):
    c = _core()
    """Per-tab per-symbol stats from the last N closed trades (rolling window)."""
    win = max(1, int(window if window is not None else c.SYMBOL_FILTER_ROLLING_WINDOW))
    cache_key = c._rolling_symbol_stats_cache_key(win)
    if _rolling_symbol_stats_cache.get("key") == cache_key:
        cached = _rolling_symbol_stats_cache.get("data")
        if cached is not None:
            return cached

    buckets: dict[str, dict[str, list]] = {}
    for entry in c.state.get("history", []):
        tab = entry.get("tab") or "Recovered"
        sym = str(entry.get("symbol") or "").strip().upper()
        if not sym:
            continue
        tab_buckets = buckets.setdefault(tab, {})
        tab_buckets.setdefault(sym, []).append(entry)

    out: dict[str, dict[str, dict]] = {}
    for tab, sym_map in buckets.items():
        tab_out: dict[str, dict] = {}
        for sym, entries in sym_map.items():
            row = c._empty_symbol_stats_row()
            for entry in entries[-win:]:
                c._accumulate_symbol_stats_row(row, entry)
            tab_out[sym] = row
        out[tab] = tab_out

    _rolling_symbol_stats_cache["key"] = cache_key
    _rolling_symbol_stats_cache["data"] = out
    return out


def _rolling_symbol_stats_for_tab(tab: str, window: int | None = None):
    c = _core()
    return c._all_rolling_symbol_stats(window).get(tab) or {}


def _default_symbol_filter_row():
    c = _core()
    return {
        "mode": "off",
        "min_trades": c.SYMBOL_FILTER_DEFAULT_MIN_TRADES,
        "min_win_rate": c.SYMBOL_FILTER_DEFAULT_MIN_WIN_RATE,
        "min_net_pnl": c.SYMBOL_FILTER_DEFAULT_MIN_NET_PNL,
    }


def _clamp_symbol_filter_row(raw: dict | None):
    c = _core()
    base = c._default_symbol_filter_row()
    if not isinstance(raw, dict):
        return base
    mode = str(raw.get("mode", base["mode"])).strip().lower()
    if mode not in c.SYMBOL_FILTER_MODES:
        mode = "off"
    try:
        min_trades = max(1, int(raw.get("min_trades", base["min_trades"])))
    except (TypeError, ValueError):
        min_trades = base["min_trades"]
    try:
        min_win_rate = float(raw.get("min_win_rate", base["min_win_rate"]))
    except (TypeError, ValueError):
        min_win_rate = base["min_win_rate"]
    min_win_rate = min(1.0, max(0.0, min_win_rate))
    try:
        min_net_pnl = float(raw.get("min_net_pnl", base["min_net_pnl"]))
    except (TypeError, ValueError):
        min_net_pnl = base["min_net_pnl"]
    return {
        "mode": mode,
        "min_trades": min_trades,
        "min_win_rate": min_win_rate,
        "min_net_pnl": min_net_pnl,
    }


def _normalize_symbol_filter_by_tab():
    c = _core()
    raw = c.state.get("symbol_filter_by_tab")
    if not isinstance(raw, dict):
        raw = {}
    out: dict[str, dict] = {}
    for tab in c.TABS:
        out[tab] = c._clamp_symbol_filter_row(raw.get(tab))
    c.state["symbol_filter_by_tab"] = out
    return out


def _effective_symbol_filter(tab: str):
    c = _core()
    return c._normalize_symbol_filter_by_tab().get(tab, c._default_symbol_filter_row())


def _normalize_symbol_list_by_tab(key: str):
    c = _core()
    raw = c.state.get(key)
    if not isinstance(raw, dict):
        raw = {}
    out: dict[str, list[str]] = {}
    for tab in c.TABS:
        items = raw.get(tab)
        if not isinstance(items, list):
            out[tab] = []
            continue
        seen: set[str] = set()
        cleaned: list[str] = []
        for item in items:
            sym = str(item or "").strip().upper()
            if not sym or sym in seen:
                continue
            seen.add(sym)
            cleaned.append(sym)
        out[tab] = cleaned
    c.state[key] = out
    return out


def _symbol_in_blocklist(tab: str, sym: str):
    c = _core()
    blocked = c._normalize_symbol_list_by_tab("symbol_blocklist_by_tab").get(tab) or []
    return sym.upper() in set(blocked)


def _symbol_passes_auto_winners(tab: str, sym: str, row: dict | None = None):
    c = _core()
    if row is None:
        row = c._rolling_symbol_stats_for_tab(tab).get(sym.upper())
    if not row:
        return False
    filt = c._effective_symbol_filter(tab)
    trades = int(row.get("trades") or 0)
    wins = int(row.get("wins") or 0)
    net = float(row.get("net_pnl") or 0)
    win_rate = (wins / trades) if trades else 0.0
    return (
        trades >= int(filt["min_trades"])
        and win_rate >= float(filt["min_win_rate"])
        and net >= float(filt["min_net_pnl"])
    )


def _auto_winner_symbols(tab: str):
    c = _core()
    tab_rows = c._rolling_symbol_stats_for_tab(tab)
    winners = [sym for sym, row in tab_rows.items() if c._symbol_passes_auto_winners(tab, sym, row)]
    winners.sort(key=lambda s: float(tab_rows[s].get("net_pnl") or 0), reverse=True)
    return winners


def _symbol_passes_filter(tab: str, sym: str):
    c = _core()
    ok, _ = c._symbol_entry_allowed(tab, sym)
    return ok


def _symbol_entry_allowed(tab: str, sym: str):
    c = _core()
    if c._symbol_in_blocklist(tab, sym):
        return False, "symbol blocklist"
    filt = c._effective_symbol_filter(tab)
    mode = filt["mode"]
    if mode == "off":
        return True, ""
    if mode == "allowlist":
        allowed = set(c._normalize_symbol_list_by_tab("symbol_allowlist_by_tab").get(tab) or [])
        if sym.upper() not in allowed:
            return False, f"not in allowlist ({len(allowed)} symbols)"
        return True, ""
    if mode == "auto_winners":
        if c._symbol_passes_auto_winners(tab, sym):
            return True, ""
        return False, "auto_winners criteria not met"
    return True, ""


def _symbol_leaderboard_rows(tab: str, limit: int = 15, board: str = "profit"):
    c = _core()
    tab_rows = c._rolling_symbol_stats_for_tab(tab)
    out: list[dict] = []
    for sym, row in tab_rows.items():
        trades = int(row.get("trades") or 0)
        wins = int(row.get("wins") or 0)
        net = float(row.get("net_pnl") or 0)
        long_pnl = float(row.get("long_pnl") or 0)
        short_pnl = float(row.get("short_pnl") or 0)
        win_rate = (wins / trades) if trades else 0.0
        dominant_side = "Long" if long_pnl >= short_pnl else "Short"
        out.append({
            "symbol": sym,
            "trades": trades,
            "wins": wins,
            "win_rate": round(win_rate, 4),
            "net_pnl": round(net, 6),
            "long_pnl": round(long_pnl, 6),
            "short_pnl": round(short_pnl, 6),
            "dominant_side": dominant_side,
            "passes_filter": c._symbol_passes_filter(tab, sym),
        })
    if board == "loss":
        filtered = [r for r in out if r["net_pnl"] < 0]
        filtered.sort(key=lambda r: (r["net_pnl"], r["win_rate"], r["trades"]))
    else:
        filtered = [r for r in out if r["net_pnl"] > 0]
        filtered.sort(key=lambda r: (r["net_pnl"], r["win_rate"], r["trades"]), reverse=True)
    return filtered[:limit]


def _dashboard_symbol_leaderboard(limit: int = 15):
    c = _core()
    board: dict[str, dict[str, list[dict]]] = {}
    for tab in c.TABS:
        top_profit = c._symbol_leaderboard_rows(tab, limit=limit, board="profit")
        top_loss = c._symbol_leaderboard_rows(tab, limit=limit, board="loss")
        if top_profit or top_loss:
            board[tab] = {"top_profit": top_profit, "top_loss": top_loss}
    return board


def _rebuild_tab_stats_for_tab(tab: str):
    c = _core()
    """Recompute one tab's trade stats from bot history (after PnL reconcile)."""
    tab_name = tab or "Recovered"
    row = c._empty_tab_stats_row()
    for entry in c.state.get("history", []):
        if (entry.get("tab") or "Recovered") != tab_name:
            continue
        c._accumulate_tab_stats_row(row, entry)
    c._normalize_tab_stats()[tab_name] = row


def _binance_close_history_enabled():
    c = _core()
    return bool(c.LIVE_MODE and c.BINANCE_CLOSE_HISTORY_ENABLED)


def _use_binance_close_cache():
    c = _core()
    return bool(c._binance_close_history_enabled() and c._BINANCE_CLOSE_HISTORY_CACHE)


def _rebuild_tab_stats_from_binance_closes():
    c = _core()
    """LIVE: trade counts / win% / best-worst from cached Binance close rows."""
    if not c._use_binance_close_cache():
        return
    tabs = {t: c._empty_tab_stats_row() for t in list(c.TABS) + ["SafeGuard", "Recovered"]}
    for row in c._BINANCE_CLOSE_HISTORY_CACHE:
        tab = row.get("tab") or "Recovered"
        if tab not in tabs:
            tabs[tab] = c._empty_tab_stats_row()
        c._accumulate_tab_stats_row(tabs[tab], row)
    c.state["tab_stats"] = tabs
    c.state["tab_stats_version"] = _TAB_STATS_VERSION
