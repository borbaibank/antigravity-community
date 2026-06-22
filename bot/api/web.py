"""FastAPI dashboard app and REST routes (extracted from bot.core)."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

import binance_live
import pionex_live
import strategies
from bot.engine.premium_config import tab17_base_universe
from config import DASHBOARD_ALLOWED_ORIGINS


def _core():
    from bot import core
    return core


@asynccontextmanager
async def lifespan(_app: FastAPI):
    c = _core()
    c._configure_library_loggers()
    c._print_startup_banner()
    os.makedirs("static", exist_ok=True)
    c._http_client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
    c._telegram_client = httpx.AsyncClient(timeout=httpx.Timeout(3.0))

    try:
        await c._refresh_dashboard_market_prices()
    except Exception as e:
        print(f"[Mark] dashboard Live Prices bootstrap error: {e}")

    bg_tasks = []
    bg_tasks.append(asyncio.create_task(c.fetch_scan_symbols()))
    bg_tasks.append(asyncio.create_task(c.refresh_scan_symbols_loop()))
    bg_tasks.append(asyncio.create_task(c.binance_ws_loop()))
    bg_tasks.append(asyncio.create_task(c.price_tick_monitor_loop()))
    bg_tasks.append(asyncio.create_task(c.price_poll_loop()))
    bg_tasks.append(asyncio.create_task(c.scheduler_loop()))
    bg_tasks.append(asyncio.create_task(c.process_watchdog_loop()))

    if c.TELEGRAM_ENABLED and (not c.TELEGRAM_BOT_TOKEN or not c.TELEGRAM_CHAT_ID):
        print("[WARN] c.TELEGRAM_ENABLED=true but BOT_TOKEN or CHAT_ID not set — notifications will be silent")

    for attempt in range(3):
        try:
            await binance_live.fetch_exchange_info(c._http_client)
            break
        except Exception as e:
            if attempt == 2:
                print(f"[WARN] Could not load exchange info after 3 attempts: {e} — using fallback precision")
            else:
                await asyncio.sleep(2 ** attempt)

    if c.LIVE_MODE:
        if c.DASHBOARD_AUTH_ENABLED and not c.DASHBOARD_PASSCODE:
            raise RuntimeError("c.DASHBOARD_PASSCODE is required when c.DASHBOARD_AUTH_ENABLED=true")
        print("[Live] LIVE MODE ENABLED — real orders will be sent to Binance")
        startup_defer = (
            c._seconds_until_next_candle_eval()
            + c.ENTRY_EVAL_BUDGET_SEC
            + c.ENTRY_BUSY_BUFFER_SEC
        )
        c._mark_entry_busy(startup_defer)
        print(
            f"[PnL Defer] Startup entry guard {startup_defer:.0f}s "
            f"(until after next candle eval + buffer)"
        )
        try:
            await binance_live.sync_server_time(c._http_client, force=True)
        except Exception as e:
            print(f"[WARN] Binance server time sync on startup failed: {e}")
        bg_tasks.append(asyncio.create_task(c.server_time_sync_loop()))
        await c.sync_live_positions()
        try:
            from bot.engine.entry import reconcile_pending_entry_orders
            await reconcile_pending_entry_orders()
        except Exception as e:
            print(f"[Limit Entry] startup reconcile error: {e}")
        try:
            await c._sync_today_income_once()
        except Exception as e:
            print(f"[Income Sync] today profit bootstrap error: {e}")
        bg_tasks.append(asyncio.create_task(c._bootstrap_daily_profit_30d()))
        bg_tasks.append(asyncio.create_task(c._repair_history_sltp_diff_once()))
        bg_tasks.append(asyncio.create_task(c._repair_history_pnl_once()))
        if c.BINANCE_CLOSE_HISTORY_ENABLED:
            bg_tasks.append(asyncio.create_task(c.binance_close_history_loop()))
        bg_tasks.append(asyncio.create_task(c._rebuild_binance_gross_breakdown(force=False)))
        bg_tasks.append(asyncio.create_task(c.user_data_stream_loop()))
        bg_tasks.append(asyncio.create_task(c.exchange_account_loop()))
        bg_tasks.append(asyncio.create_task(c.income_sync_loop()))
        if c.ENTRY_ORDER_STYLE == "limit":
            bg_tasks.append(asyncio.create_task(c.entry_limit_ttl_loop()))
    else:
        print("[Paper] Paper trading mode — no real orders")
    bg_tasks.append(asyncio.create_task(c.entry_retry_loop()))
    if c.PIONEX_CONFIGURED:
        bg_tasks.append(asyncio.create_task(c.pionex_balance_loop()))
        print("[Pionex] Balance card enabled — polling wallet/balancesFull")

    try:
        yield
    finally:
        print("Shutting down background tasks...")
        for task in bg_tasks:
            task.cancel()
        await asyncio.gather(*bg_tasks, return_exceptions=True)
        await c._http_client.aclose()
        if c._telegram_client is not None:
            await c._telegram_client.aclose()

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=DASHBOARD_ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def no_cache_dashboard_assets(request: Request, call_next):
    c = _core()
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

app.mount("/static", StaticFiles(directory="static"), name="static")

def _check_auth(request: Request):
    c = _core()
    if not c.DASHBOARD_AUTH_ENABLED:
        return
    token = request.headers.get("X-Passcode") or request.query_params.get("token", "")
    if token != c.DASHBOARD_PASSCODE:
        raise HTTPException(status_code=401, detail="Unauthorized")

def _require_testnet():
    c = _core()
    """Block dangerous test endpoints on mainnet."""
    if not c.BINANCE_TESTNET:
        raise HTTPException(
            status_code=403,
            detail="Test endpoints disabled on mainnet (set c.BINANCE_TESTNET=true to allow)",
        )

def _dashboard_market_prices():
    c = _core()
    """Mark (or last) prices for dashboard header ticker."""
    out: dict[str, float] = {}
    for sym in c._DASHBOARD_MARKET_SYMBOLS:
        p = c._mark_or_last(sym)
        if p is not None:
            out[sym] = p
    return out


def _dashboard_margin_history():
    c = _core()
    """Return margin-balance equity history, seeding reset dashboards from live balance."""
    if not c.LIVE_MODE:
        return []

    mh = list(c.state.get("margin_history") or [])
    if mh:
        return mh

    # After a dashboard reset, start the equity curve from the current account
    # margin balance instead of showing an empty trade-history based curve.
    for key in ("totalMarginBalance", "marginBalance", "totalWalletBalance", "walletBalance"):
        try:
            value = c.exchange_account.get(key)
            if value is not None:
                return [{
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "value": float(value),
                }]
        except (TypeError, ValueError):
            continue
    return []


@app.get("/")
def get_dashboard():
    c = _core()
    with open("static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


def _dashboard_live_prices(*, symbols: set[str] | None = None):
    c = _core()
    """Mark/last prices for exchange legs; optional symbol filter (lite core tier)."""
    if not c.LIVE_MODE:
        return {}
    out: dict = {}
    for p in (c.exchange_account.get("positions") or []):
        sym = p.get("symbol")
        if not sym:
            continue
        if symbols is not None and sym not in symbols:
            continue
        px = c._mark_or_last(sym) or p.get("markPrice")
        if px is not None:
            out[sym] = px
    return out


def _lite_core_price_symbols():
    c = _core()
    """Symbols needed for mobile core WS ticks (open legs + market cards)."""
    syms = {"BTCUSDT", "ETHUSDT", "XAUUSDT", "CLUSDT"}
    for pos in (c.state.get("open_positions") or {}).values():
        sym = pos.get("symbol")
        if sym:
            syms.add(sym)
    if c.LIVE_MODE:
        for p in (c.exchange_account.get("positions") or []):
            sym = p.get("symbol")
            if sym:
                syms.add(sym)
    return syms


def _build_dashboard_ws_payload(*, tier: str = "full"):
    c = _core()
    """Build dashboard WebSocket payload.

    tier ``full`` — desktop default; lite refresh every ~2 min.
    tier ``core`` — lite ticks: positions, balances, prices, health, strategy stats (no history/config extras).
    """
    full_tier = tier == "full"
    price_symbols = _lite_core_price_symbols() if not full_tier else None
    payload: dict = {
        "balances": c.state["balances"],
        "unrealized_pnls": c.state["unrealized_pnls"],
        "open_positions": c.state["open_positions"],
        "market_prices": _dashboard_market_prices(),
        "latest_prices": {k: c._position_price(v)
                          for k, v in c.state["open_positions"].items()},
        "live_prices": _dashboard_live_prices(symbols=price_symbols),
        "live_mode": c.LIVE_MODE,
        "testnet": c.BINANCE_TESTNET,
        "uds_connected": c._uds_connected,
        "exchange_account": c._dashboard_exchange_account(),
        "pionex_balance": c._dashboard_pionex_balance(),
        "pnl_summary": c._dashboard_pnl_summary(),
        "sync_issues": c.state.get("sync_issues", []),
        "error_events": c.state.get("error_events", [])[-(20 if full_tier else 5):],
        "health": c._health_snapshot(),
        "circuit_breaker": c._circuit_breaker,
        "algo_orders_used": c._dashboard_algo_order_count,
        "algo_orders_max": c._max_open_algo_orders(),
        "binance_rate_limit": c._binance_rate_limit_snapshot(),
        "_tier": tier,
    }
    payload.update({
        "tab_stats": c.state.get("tab_stats") or {},
        "tab_enabled": c.state.get("tab_enabled", {tab: False for tab in c.TABS}),
        "tab_timeframes": c.TAB_TIMEFRAMES,
    })
    from bot.engine.premium_hooks import premium_loaded
    payload["premium_loaded"] = premium_loaded()
    payload["edition"] = "pro" if premium_loaded() else "community"
    if c.LIVE_MODE:
        payload["binance_income"] = c.state.get("binance_income")
    elif not full_tier:
        payload["stats_trade_history"] = c._dashboard_stats_trade_history()
    if full_tier:
        payload.update({
            "stats_meta": c._dashboard_stats_meta(),
            "binance_api_trades": c._BINANCE_RAW_TRADES_CACHE[:50],
            "sltp_mode": c._effective_sltp_mode(),
            "local_sltp": c._effective_local_sltp(),
            "order_env": c.ORDER_ENV,
            "api_account_type": c.BINANCE_API_ACCOUNT_TYPE,
            "price_feed_env": c.PRICE_FEED_ENV,
            "equity_margin_baseline": c.EQUITY_CURVE_MARGIN_BASELINE if c.LIVE_MODE else 0.0,
            "long_short_balance_mode": c.state.get("long_short_balance_mode", "off"),
            "trade_side_mode": c.state.get("trade_side_mode", "both"),
            "max_positions_per_tab": c._effective_max_positions(),
            "margin_size": c._effective_margin_size(),
            "leverage": c._effective_leverage(),
            "notional_size": c._effective_notional_size(),
            "initial_balance": c.INITIAL_BALANCE,
            "symbol_scan_limit": c._effective_symbol_scan_limit(),
            "symbol_scan_limit_by_tab": c._normalize_symbol_scan_limit_by_tab(),
            "symbol_scan_count": len(c.SCAN_SYMBOLS),
            "symbol_filter_by_tab": c._normalize_symbol_filter_by_tab(),
            "symbol_allowlist_by_tab": c._normalize_symbol_list_by_tab("symbol_allowlist_by_tab"),
            "symbol_blocklist_by_tab": c._normalize_symbol_list_by_tab("symbol_blocklist_by_tab"),
            "symbol_filter_rolling_window": c.SYMBOL_FILTER_ROLLING_WINDOW,
            "daily_profit_30d": c._dashboard_daily_profit_30d(),
            "daily_loss_usd": c._daily_loss_usd,
            "recent_history": c._dashboard_recent_history(),
            "stats_trade_history": c._dashboard_stats_trade_history(),
            "equity_close_series": c._dashboard_equity_close_series(),
            "equity_snapshots": (c.state.get("equity_snapshots") or [])[-500:] if c.LIVE_MODE else [],
            "binance_recent_history": c._dashboard_binance_recent_history(),
            "recent_history_source": c._dashboard_recent_history_source(),
            "margin_history": _dashboard_margin_history(),
            "symbol_leaderboard": c._dashboard_symbol_leaderboard(),
        })
    return payload


_DASHBOARD_LITE_SLEEP_SEC = 8
_DASHBOARD_LITE_FULL_EVERY = 15  # full payload every ~2 min at 8s ticks


@app.websocket("/ws")
async def dashboard_ws(websocket: WebSocket, token: str = "", lite: bool = False):
    c = _core()
    c = _core()
    c = _core()
    if c.DASHBOARD_AUTH_ENABLED and token != c.DASHBOARD_PASSCODE:
        await websocket.close(code=4001)
        return
    await websocket.accept()
    tick = 0
    try:
        while True:
            c._refresh_exchange_account_marks()
            c._recalculate_unrealized_pnls()
            if lite:
                tier = "full" if tick == 0 or (tick % _DASHBOARD_LITE_FULL_EVERY == 0) else "core"
            else:
                tier = "full"
            payload = _build_dashboard_ws_payload(tier=tier)
            tick += 1
            try:
                await websocket.send_json(payload)
            except WebSocketDisconnect:
                break
            except Exception as send_err:
                if type(send_err).__name__ == "ConnectionClosed":
                    break
                raise
            await asyncio.sleep(_DASHBOARD_LITE_SLEEP_SEC if lite else 1)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        if type(e).__name__ != "ConnectionClosed":
            print(f"WS push error: {e}")

@app.get("/api/history")
async def api_history(
    tab: str = "All",
    offset: int = 0,
    limit: int = 50,
    days: int = 0,
    _=Depends(_check_auth),
):
    """Paginated bot close history for dashboard (newest first; days=0 = all time in state)."""
    c = _core()
    try:
        if tab not in ("All", *c.TABS, "Recovered", "SafeGuard"):
            return JSONResponse({"error": f"Invalid tab: {tab}"}, status_code=400)
        payload = c._paginated_history_page(
            tab=None if tab == "All" else tab,
            offset=offset,
            limit=limit,
            days=days,
        )
        return JSONResponse(payload)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/equity-curve")
async def api_equity_curve(
    tab: str = "All",
    days: int = 0,
    max_points: int = 0,
    _=Depends(_check_auth),
):
    """Equity curve from full close history (days=0 = all time; downsampled for chart)."""
    c = _core()
    try:
        if tab not in ("All", *c.TABS, "Recovered", "SafeGuard"):
            return JSONResponse({"error": f"Invalid tab: {tab}"}, status_code=400)
        cap = int(max_points or 0) or int(c.DASHBOARD_EQUITY_CURVE_MAX_POINTS or 2000)
        page_max = max(100, int(getattr(c, "DASHBOARD_EQUITY_CURVE_MAX_POINTS", 2000) or 2000))
        cap = max(50, min(cap, page_max))
        payload = c._dashboard_equity_curve_api(
            tab=None if tab == "All" else tab,
            days=days,
            max_points=cap,
        )
        return JSONResponse(payload)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/strategy-stats")
async def api_strategy_stats(
    days: int = 0,
    _=Depends(_check_auth),
):
    """Strategy Performance stats from full close history (days=0 = all time in state)."""
    c = _core()
    try:
        return JSONResponse(c._dashboard_strategy_stats_api(days=days))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/trades")
async def api_trades(
    symbol: str = None,
    limit: int = 50,
    refresh: bool = False,
    _=Depends(_check_auth),
):
    """Recent fills from server cache (userTrades when enabled, else bot history)."""
    c = _core()
    if not c.LIVE_MODE:
        return JSONResponse({"error": "Only available in c.LIVE_MODE"}, status_code=400)
    try:
        if not c.BINANCE_CLOSE_HISTORY_ENABLED:
            rows = c._history_rows_as_api_trades(limit=max(limit, 1))
            if symbol:
                rows = [r for r in rows if r.get("symbol") == symbol]
            return JSONResponse({
                "trades": rows[:limit],
                "cached": True,
                "stale": False,
                "source": "bot_history",
                "fetched_at": 0,
            })
        import time as _time_mod
        now = _time_mod.monotonic()
        stale = (
            not c._BINANCE_RAW_TRADES_CACHE
            or (now - c._BINANCE_CLOSE_HISTORY_FETCHED_AT) >= c.BINANCE_CLOSE_HISTORY_TTL_SEC
        )
        if refresh or stale:
            if not c._binance_rate_limited():
                asyncio.create_task(c._refresh_binance_close_history(force=refresh))
        rows = list(c._BINANCE_RAW_TRADES_CACHE)
        if symbol:
            rows = [r for r in rows if r.get("symbol") == symbol]
        return JSONResponse({
            "trades": rows[:limit],
            "cached": True,
            "stale": stale,
            "source": "binance_user_trades",
            "fetched_at": c._BINANCE_CLOSE_HISTORY_FETCHED_AT,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/scan")
async def api_scan(_=Depends(_check_auth)):
    c = _core()
    c = _core()
    """Manually trigger scan + staggered entries (same flow as auto scheduler)."""
    if c._eval_lock.locked():
        return JSONResponse({"status": "busy", "msg": "Scan already running"})
    async with c._eval_lock:
        await c.check_invalidations_loop(force=True)
        c._begin_entry_window()
        c._reset_entry_stagger()
        try:
            scan_4h = await c.scan_candle_signals("4h")
            scan_1h = await c.scan_candle_signals("1h")
            if scan_4h:
                await c.execute_scanned_entries(scan_4h)
            if scan_1h:
                await c.execute_scanned_entries(scan_1h)
        finally:
            c._release_entry_busy_after_eval()
    return JSONResponse({"status": "ok", "msg": "Scan complete"})


@app.post("/api/tab-enabled")
async def api_tab_enabled(payload: dict, _=Depends(_check_auth)):
    c = _core()
    c = _core()
    """Toggle signal generation on/off for a tab.

    Body: {"tab": "Tab1", "enabled": true|false}
    Disabling only stops NEW signal entries; open positions continue to be managed.
    """
    tab = str(payload.get("tab", ""))
    if tab not in c.TABS:
        return JSONResponse({"status": "error", "msg": f"unknown tab: {tab}"}, status_code=400)
    enabled = bool(payload.get("enabled", False))
    c.state.setdefault("tab_enabled", {tab2: False for tab2 in c.TABS})[tab] = enabled
    await c.save_state()
    print(f"[Tab Switch] {tab} → {'ON' if enabled else 'OFF'}")
    return JSONResponse({"status": "ok", "tab": tab, "enabled": enabled})


@app.post("/api/tabs-enabled-all")
async def api_tabs_enabled_all(payload: dict, _=Depends(_check_auth)):
    c = _core()
    c = _core()
    """Enable or disable signal generation for every strategy tab."""
    enabled = bool(payload.get("enabled", False))
    c.state["tab_enabled"] = {tab: enabled for tab in c.TABS}
    await c.save_state()
    print(f"[Tab Switch] ALL → {'ON' if enabled else 'OFF'}")
    return JSONResponse({"status": "ok", "enabled": enabled, "tabs": list(c.TABS)})

@app.post("/api/long-short-balance")
async def api_long_short_balance(payload: dict, _=Depends(_check_auth)):
    c = _core()
    c = _core()
    mode = str(payload.get("mode", "off")).strip().lower()
    if mode not in {"nearly", "cap", "off"}:
        return JSONResponse({"status": "error", "msg": f"unknown mode: {mode}"}, status_code=400)
    c.state["long_short_balance_mode"] = mode
    await c.save_state()
    print(f"[Long/Short Balance] mode={mode}")
    return JSONResponse({"status": "ok", "mode": mode})

@app.post("/api/trade-side-mode")
async def api_trade_side_mode(payload: dict, _=Depends(_check_auth)):
    c = _core()
    c = _core()
    mode = str(payload.get("mode", "both")).strip().lower()
    if mode not in {"both", "long_only", "short_only"}:
        return JSONResponse({"status": "error", "msg": f"unknown mode: {mode}"}, status_code=400)
    c.state["trade_side_mode"] = mode
    await c.save_state()
    print(f"[Trade Side Mode] mode={mode}")
    return JSONResponse({"status": "ok", "mode": mode})


@app.post("/api/symbol-filter")
async def api_symbol_filter(payload: dict, _=Depends(_check_auth)):
    c = _core()
    c = _core()
    tab = str(payload.get("tab", "")).strip()
    if tab not in c.TABS:
        return JSONResponse({"status": "error", "msg": f"unknown tab: {tab}"}, status_code=400)
    filt = c._normalize_symbol_filter_by_tab()
    row = dict(filt[tab])
    if "mode" in payload:
        mode = str(payload.get("mode", "off")).strip().lower()
        if mode not in c.SYMBOL_FILTER_MODES:
            return JSONResponse({"status": "error", "msg": f"unknown mode: {mode}"}, status_code=400)
        row["mode"] = mode
    if "min_trades" in payload:
        try:
            row["min_trades"] = max(1, int(payload.get("min_trades")))
        except (TypeError, ValueError):
            return JSONResponse({"status": "error", "msg": "min_trades must be an integer"}, status_code=400)
    if "min_win_rate" in payload:
        try:
            row["min_win_rate"] = min(1.0, max(0.0, float(payload.get("min_win_rate"))))
        except (TypeError, ValueError):
            return JSONResponse({"status": "error", "msg": "min_win_rate must be a number"}, status_code=400)
    if "min_net_pnl" in payload:
        try:
            row["min_net_pnl"] = float(payload.get("min_net_pnl"))
        except (TypeError, ValueError):
            return JSONResponse({"status": "error", "msg": "min_net_pnl must be a number"}, status_code=400)
    filt[tab] = c._clamp_symbol_filter_row(row)
    c.state["symbol_filter_by_tab"] = filt
    await c.save_state()
    auto = c._auto_winner_symbols(tab)
    print(
        f"[Symbol Filter] {tab} mode={filt[tab]['mode']} "
        f"min_trades={filt[tab]['min_trades']} min_wr={filt[tab]['min_win_rate']:.0%} "
        f"min_pnl=${filt[tab]['min_net_pnl']:.2f} auto_winners={len(auto)}"
    )
    return JSONResponse({
        "status": "ok",
        "tab": tab,
        "filter": filt[tab],
        "auto_winner_count": len(auto),
        "auto_winners": auto[:100],
    })


@app.post("/api/symbol-allowlist")
async def api_symbol_allowlist(payload: dict, _=Depends(_check_auth)):
    c = _core()
    c = _core()
    tab = str(payload.get("tab", "")).strip()
    if tab not in c.TABS:
        return JSONResponse({"status": "error", "msg": f"unknown tab: {tab}"}, status_code=400)
    action = str(payload.get("action", "set")).strip().lower()
    allow = c._normalize_symbol_list_by_tab("symbol_allowlist_by_tab")
    if action == "clear":
        allow[tab] = []
    elif action == "apply_auto_winners":
        allow[tab] = c._auto_winner_symbols(tab)
        filt = c._normalize_symbol_filter_by_tab()
        filt[tab] = c._clamp_symbol_filter_row({**filt[tab], "mode": "allowlist"})
        c.state["symbol_filter_by_tab"] = filt
    elif action == "set":
        raw = payload.get("symbols")
        if not isinstance(raw, list):
            return JSONResponse({"status": "error", "msg": "symbols must be a list"}, status_code=400)
        seen: set[str] = set()
        cleaned: list[str] = []
        for item in raw:
            sym = str(item or "").strip().upper()
            if not sym or sym in seen:
                continue
            seen.add(sym)
            cleaned.append(sym)
        allow[tab] = cleaned
    else:
        return JSONResponse({"status": "error", "msg": f"unknown action: {action}"}, status_code=400)
    c.state["symbol_allowlist_by_tab"] = allow
    await c.save_state()
    print(f"[Symbol Allowlist] {tab} action={action} count={len(allow[tab])}")
    return JSONResponse({
        "status": "ok",
        "tab": tab,
        "symbols": allow[tab],
        "filter": c._effective_symbol_filter(tab),
    })


@app.post("/api/symbol-blocklist")
async def api_symbol_blocklist(payload: dict, _=Depends(_check_auth)):
    c = _core()
    c = _core()
    tab = str(payload.get("tab", "")).strip()
    if tab not in c.TABS:
        return JSONResponse({"status": "error", "msg": f"unknown tab: {tab}"}, status_code=400)
    action = str(payload.get("action", "set")).strip().lower()
    blocked = c._normalize_symbol_list_by_tab("symbol_blocklist_by_tab")
    if action == "clear":
        blocked[tab] = []
    elif action == "set":
        raw = payload.get("symbols")
        if not isinstance(raw, list):
            return JSONResponse({"status": "error", "msg": "symbols must be a list"}, status_code=400)
        seen: set[str] = set()
        cleaned: list[str] = []
        for item in raw:
            sym = str(item or "").strip().upper()
            if not sym or sym in seen:
                continue
            seen.add(sym)
            cleaned.append(sym)
        blocked[tab] = cleaned
    else:
        return JSONResponse({"status": "error", "msg": f"unknown action: {action}"}, status_code=400)
    c.state["symbol_blocklist_by_tab"] = blocked
    await c.save_state()
    print(f"[Symbol Blocklist] {tab} action={action} count={len(blocked[tab])}")
    return JSONResponse({"status": "ok", "tab": tab, "symbols": blocked[tab]})


@app.post("/api/sltp-mode")
async def api_sltp_mode(payload: dict, _=Depends(_check_auth)):
    c = _core()
    c = _core()
    if not c.LIVE_MODE:
        return JSONResponse({"status": "error", "msg": "Only available in c.LIVE_MODE"}, status_code=400)
    mode = c._normalize_sltp_mode(payload.get("mode"))
    if str(payload.get("mode", "")).strip().lower() not in c.SLTP_MODES:
        return JSONResponse(
            {"status": "error", "msg": f"mode must be one of {sorted(c.SLTP_MODES)}"},
            status_code=400,
        )
    c.state["sltp_mode"] = mode
    c.state.pop("local_sltp", None)
    await c.save_state()
    print(f"[SL/TP Mode] {mode}")
    return JSONResponse({
        "status": "ok",
        "sltp_mode": mode,
        "local_sltp": mode == "local",
        "algo_orders_max": c._max_open_algo_orders(),
    })


@app.post("/api/local-sltp")
async def api_local_sltp(payload: dict, _=Depends(_check_auth)):
    c = _core()
    c = _core()
    """Legacy boolean toggle — maps to local/binance only."""
    if not c.LIVE_MODE:
        return JSONResponse({"status": "error", "msg": "Only available in c.LIVE_MODE"}, status_code=400)
    enabled_raw = payload.get("enabled")
    if enabled_raw is None:
        return JSONResponse({"status": "error", "msg": "enabled is required (true|false)"}, status_code=400)
    enabled = bool(enabled_raw)
    mode = "local" if enabled else "binance"
    c.state["sltp_mode"] = mode
    c.state.pop("local_sltp", None)
    await c.save_state()
    mode_label = "local (bot-managed)" if enabled else "binance (exchange algo)"
    print(f"[SL/TP Mode] {mode_label} (legacy API)")
    return JSONResponse({"status": "ok", "local_sltp": enabled, "sltp_mode": mode})


@app.post("/api/max-positions")
async def api_max_positions(payload: dict, _=Depends(_check_auth)):
    c = _core()
    try:
        value = int(payload.get("value", c.MAX_POSITIONS_PER_TAB))
    except (TypeError, ValueError):
        return JSONResponse({"status": "error", "msg": "value must be an integer"}, status_code=400)
    if value not in c.MAX_POSITIONS_OPTIONS:
        return JSONResponse(
            {"status": "error", "msg": f"value must be one of {list(c.MAX_POSITIONS_OPTIONS)}"},
            status_code=400,
        )
    c.state["max_positions_per_tab"] = value
    await c.save_state()
    trimmed = 0
    if c.LIVE_MODE and c.ENTRY_ORDER_STYLE == "limit":
        for tab in c.TABS:
            trimmed += await c._trim_pending_entries_for_tab(tab)
    print(f"[Risk] max_positions_per_tab={value}" + (f" trimmed_pending={trimmed}" if trimmed else ""))
    return JSONResponse({"status": "ok", "max_positions_per_tab": value, "trimmed_pending": trimmed})


_SIZING_ENV_ONLY_MSG = (
    "Sizing (notional, leverage, margin) is configured via .env only — restart server after changes."
)


@app.post("/api/notional-size")
async def api_notional_size(payload: dict, _=Depends(_check_auth)):
    return JSONResponse({"status": "error", "msg": _SIZING_ENV_ONLY_MSG}, status_code=403)


@app.post("/api/leverage")
async def api_leverage(payload: dict, _=Depends(_check_auth)):
    return JSONResponse({"status": "error", "msg": _SIZING_ENV_ONLY_MSG}, status_code=403)


@app.post("/api/margin-size")
async def api_margin_size(payload: dict, _=Depends(_check_auth)):
    return JSONResponse({"status": "error", "msg": _SIZING_ENV_ONLY_MSG}, status_code=403)


@app.post("/api/symbol-scan-limit")
async def api_symbol_scan_limit(payload: dict, _=Depends(_check_auth)):
    c = _core()
    c = _core()
    try:
        value = int(payload.get("value", c.SYMBOL_SCAN_LIMIT))
    except (TypeError, ValueError):
        return JSONResponse({"status": "error", "msg": "value must be an integer"}, status_code=400)
    tab = str(payload.get("tab") or "").strip()
    allowed = set(c.SYMBOL_SCAN_OPTIONS)
    cap = tab17_base_universe()
    if tab == "Tab17" and cap is not None:
        allowed.add(cap)
    if value not in allowed:
        return JSONResponse(
            {"status": "error", "msg": f"value must be one of {sorted(allowed)}"},
            status_code=400,
        )
    if tab:
        if tab not in c.TABS:
            return JSONResponse({"status": "error", "msg": f"unknown tab: {tab}"}, status_code=400)
        if tab == "Tab7":
            return JSONResponse(
                {"status": "error", "msg": "per-tab scan override is not supported for Tab7"},
                status_code=400,
            )
        by_tab = c.state.setdefault("symbol_scan_limit_by_tab", {})
        if value == c._effective_symbol_scan_limit() and tab in by_tab:
            by_tab.pop(tab, None)
        else:
            by_tab[tab] = value
        c.state["symbol_scan_limit_by_tab"] = c._normalize_symbol_scan_limit_by_tab()
        print(f"[Scan] symbol_scan_limit_by_tab[{tab}]={value}")
    else:
        c.state["symbol_scan_limit"] = value
        print(f"[Scan] symbol_scan_limit={value}")
    await c.save_state()
    await c.fetch_scan_symbols()
    print(f"[Scan] universe loaded ({len(c.SCAN_SYMBOLS)} symbols)")
    return JSONResponse({
        "status": "ok",
        "symbol_scan_limit": c._effective_symbol_scan_limit(),
        "symbol_scan_limit_by_tab": c._normalize_symbol_scan_limit_by_tab(),
        "symbol_scan_count": len(c.SCAN_SYMBOLS),
        "tab": tab or None,
    })


@app.post("/api/sync-issues/clear")
async def api_clear_sync_issues(_=Depends(_check_auth)):
    c = _core()
    c = _core()
    """Clear all live-sync issues from the dashboard panel."""
    cleared = len(c.state.get("sync_issues", []))
    c.state["sync_issues"] = []
    await c.save_state()
    return JSONResponse({"status": "ok", "cleared": cleared})


@app.post("/api/health/warnings/clear")
async def api_clear_health_warnings(_=Depends(_check_auth)):
    c = _core()
    c = _core()
    """Clear persisted dashboard health warnings that are safe to acknowledge."""
    cleared_sync = len(c.state.get("sync_issues", []))
    cleared_events = len(c.state.get("error_events", []))
    c.state["sync_issues"] = []
    c.state["error_events"] = []
    c._error_event_recent.clear()
    await c.save_state()
    return JSONResponse({
        "status": "ok",
        "cleared_sync_issues": cleared_sync,
        "cleared_error_events": cleared_events,
    })


def _group_position_keys_by_leg(pos_keys: list[str]):
    c = _core()
    """Map position keys to (symbol, positionSide) hedge legs."""
    leg_map: dict[tuple[str, str], list[str]] = {}
    for pk in pos_keys:
        pos = c.state["open_positions"].get(pk)
        if not pos:
            continue
        leg_map.setdefault(c._position_tuple_from_state(pos), []).append(pk)
    return leg_map


async def _emergency_close_retry_worker():
    c = _core()
    """Wait then retry dashboard emergency close for positions still open."""
    try:
        await asyncio.sleep(c.CLOSE_ALL_RETRY_SEC)
        pending = c._EMERGENCY_CLOSE_RETRY_PENDING
        c._EMERGENCY_CLOSE_RETRY_PENDING = None
        if not pending:
            return
        keys = [pk for pk in pending.get("keys") or [] if pk in c.state["open_positions"]]
        if not keys:
            return
        print(
            f"[Emergency] Rate-limit retry: closing {len(keys)} remaining position(s) "
            f"({'full leg' if pending.get('full_leg') else 'strategy'})"
        )
        result = await _emergency_close_batch(
            keys,
            full_leg=bool(pending.get("full_leg")),
            skip_preflight=True,
        )
        if result.get("status") == "rate_limited":
            remaining = [pk for pk in keys if pk in c.state["open_positions"]]
            if remaining:
                c._schedule_emergency_close_retry(remaining, bool(pending.get("full_leg")))
        elif result.get("closed") or result.get("stale_removed"):
            print(
                f"[Emergency] Retry closed {result.get('closed', 0)} + "
                f"stale-removed {result.get('stale_removed', 0)} position(s)"
            )
    finally:
        c._EMERGENCY_CLOSE_RETRY_TASK = None


def _schedule_emergency_close_retry(pos_keys: list[str], full_leg: bool):
    c = _core()
    """Schedule (or merge) a dashboard close retry after c.CLOSE_ALL_RETRY_SEC."""
    remaining = [pk for pk in pos_keys if pk in c.state["open_positions"]]
    if not remaining:
        return

    if c._EMERGENCY_CLOSE_RETRY_PENDING:
        merged = list(dict.fromkeys(c._EMERGENCY_CLOSE_RETRY_PENDING.get("keys") or []) + remaining)
        c._EMERGENCY_CLOSE_RETRY_PENDING = {
            "keys": merged,
            "full_leg": bool(c._EMERGENCY_CLOSE_RETRY_PENDING.get("full_leg")) and full_leg,
        }
        print(
            f"[Emergency] Close retry already scheduled — merged to {len(merged)} position(s) "
            f"in {c.CLOSE_ALL_RETRY_SEC}s"
        )
        return

    c._EMERGENCY_CLOSE_RETRY_PENDING = {"keys": remaining, "full_leg": full_leg}
    print(
        f"[Emergency] Rate limited — will retry closing {len(remaining)} position(s) "
        f"in {c.CLOSE_ALL_RETRY_SEC}s"
    )
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    c._EMERGENCY_CLOSE_RETRY_TASK = loop.create_task(c._emergency_close_retry_worker())


async def _finalize_closed_positions(
    pos_keys: list[str],
    *,
    sym: str,
    leg_was_flat: bool,
    live_qty_cache: dict[tuple[str, str], float] | None,
    close_order_id: int | None,
    result: dict,
):
    c = _core()
    """Remove bot c.state for positions after exchange leg is flat or market-closed."""
    for pk in pos_keys:
        if pk not in c.state["open_positions"]:
            continue
        pos = c.state["open_positions"][pk]
        price = c._mark_or_last(sym) or pos["entry_price"]
        await c._close_position_unsafe(
            pk,
            price,
            "Manual",
            skip_exchange=True,
            live_qty_cache=live_qty_cache,
            close_order_id=close_order_id,
        )
        if pk not in c.state["open_positions"]:
            if leg_was_flat:
                result["stale_removed"] += 1
            else:
                result["closed"] += 1
        elif pk not in result["failed"]:
            result["failed"].append(pk)


async def _emergency_close_batch(
    pos_keys: list[str],
    *,
    full_leg: bool,
    skip_preflight: bool = False,
):
    c = _core()
    """Close many positions with minimal REST usage: one market order per hedge leg."""
    result: dict = {"closed": 0, "stale_removed": 0, "failed": [], "legs": 0}

    if not pos_keys:
        return result

    if not c.LIVE_MODE:
        async with c._state_lock:
            for pk in list(pos_keys):
                if pk not in c.state["open_positions"]:
                    continue
                pos = c.state["open_positions"][pk]
                price = c._mark_or_last(pos["symbol"]) or pos["entry_price"]
                await c._close_position_unsafe(pk, price, "Manual")
                if pk not in c.state["open_positions"]:
                    result["closed"] += 1
                else:
                    result["failed"].append(pk)
        return result

    if c.CLOSE_ALL_PREFLIGHT and not skip_preflight and c._binance_rate_limited():
        snap = c._binance_rate_limit_snapshot()
        result["status"] = "rate_limited"
        result["failed"] = list(pos_keys)
        result["rate_limit"] = snap
        c._schedule_emergency_close_retry(list(pos_keys), full_leg)
        return result

    async with c._state_lock:
        leg_map = _group_position_keys_by_leg(pos_keys)
    legs = list(leg_map.keys())
    result["legs"] = len(legs)
    if not legs:
        return result

    c._mark_entry_busy(max(60.0, len(legs) * c.CLOSE_ALL_STAGGER_SEC + 30.0))

    live_qty_cache = await c._fetch_live_qty_cache()

    for leg_idx, leg in enumerate(legs):
        if leg_idx > 0 and c.CLOSE_ALL_STAGGER_SEC > 0:
            await asyncio.sleep(c.CLOSE_ALL_STAGGER_SEC)

        sym, pos_side = leg
        keys_on_leg = leg_map[leg]

        async with c._state_lock:
            positions = [
                (pk, c.state["open_positions"][pk])
                for pk in keys_on_leg
                if pk in c.state["open_positions"]
            ]
        if not positions:
            continue

        live_qty = float(live_qty_cache.get(leg, 0.0))
        leg_was_flat = live_qty <= 1e-9

        if leg_was_flat:
            async with c._state_lock:
                await c._finalize_closed_positions(
                    keys_on_leg,
                    sym=sym,
                    leg_was_flat=True,
                    live_qty_cache=live_qty_cache,
                    close_order_id=None,
                    result=result,
                )
            continue

        if full_leg:
            close_qty = binance_live.round_qty(sym, live_qty)
            finalize_keys = None
        else:
            async with c._state_lock:
                sibling_qty = sum(
                    float(p.get("qty") or 0)
                    for pk, p in c.state["open_positions"].items()
                    if pk not in keys_on_leg and c._position_tuple_from_state(p) == leg
                )
                target_qty = sum(float(p.get("qty") or 0) for _, p in positions)
            max_close = max(0.0, live_qty - sibling_qty)
            close_qty = binance_live.round_qty(sym, min(target_qty, max_close))
            finalize_keys = [pk for pk, _ in positions]

        if close_qty <= 1e-9:
            async with c._state_lock:
                await c._finalize_closed_positions(
                    finalize_keys or keys_on_leg,
                    sym=sym,
                    leg_was_flat=True,
                    live_qty_cache=live_qty_cache,
                    close_order_id=None,
                    result=result,
                )
            continue

        if c._binance_rate_limited():
            result["status"] = "rate_limited"
            result["rate_limit"] = c._binance_rate_limit_snapshot()
            remaining = [pk for pk in pos_keys if pk in c.state["open_positions"]]
            result["failed"] = remaining
            c._schedule_emergency_close_retry(remaining, full_leg)
            break

        close_side = "SELL" if pos_side == "LONG" else "BUY"
        tab = str(positions[0][1].get("tab") or "Tab1")
        close_order_id = None
        exchange_ok = False

        try:
            if full_leg:
                await binance_live.cancel_all_algo_orders(c._http_client, sym, pos_side)
            else:
                async with c._state_lock:
                    for pk in finalize_keys or []:
                        pos = c.state["open_positions"].get(pk)
                        if not pos:
                            continue
                        for oid_key in ("sl_order_id", "tp_order_id"):
                            oid = pos.get(oid_key)
                            if not oid:
                                continue
                            try:
                                await binance_live.cancel_algo_order(c._http_client, algo_id=oid)
                            except Exception as e:
                                print(f"[Emergency] cancel {oid_key} {oid} for {pk}: {e}")

            close_client_id = c._strategy_client_id(tab, pos_side, "CLOSE")
            close_res = await binance_live.place_market_order(
                c._http_client,
                sym,
                close_side,
                close_qty,
                position_side=pos_side,
                client_order_id=close_client_id,
            )
            close_order_id = int(close_res.get("orderId") or 0) or None
            c._record_bot_close_fill(sym, pos_side, close_side, close_qty, close_client_id)
            live_qty_cache[leg] = max(0.0, live_qty - close_qty)
            exchange_ok = True
        except Exception as e:
            if c._is_binance_rate_limit(e):
                c._note_binance_rate_limit(e)
                result["status"] = "rate_limited"
                result["rate_limit"] = c._binance_rate_limit_snapshot()
                remaining = [pk for pk in pos_keys if pk in c.state["open_positions"]]
                for pk in remaining:
                    if pk not in result["failed"]:
                        result["failed"].append(pk)
                print(f"[Emergency] rate limited closing {sym} {pos_side} — stopping batch")
                c._schedule_emergency_close_retry(remaining, full_leg)
                break
            c._note_binance_rate_limit(e)
            try:
                fresh_cache = await c._fetch_live_qty_cache()
                live_qty_cache.update(fresh_cache)
            except Exception as refresh_err:
                print(f"[Emergency] leg qty refresh failed for {sym} {pos_side}: {refresh_err}")
            if float(live_qty_cache.get(leg, live_qty)) <= 1e-9:
                exchange_ok = True
                leg_was_flat = True
            else:
                print(f"[Emergency] market close failed for {sym} {pos_side}: {e}")
                result["failed"].extend([pk for pk, _ in positions if pk not in result["failed"]])
                continue

        if not exchange_ok:
            continue

        async with c._state_lock:
            if full_leg:
                keys_to_finalize = [
                    pk
                    for pk, p in c.state["open_positions"].items()
                    if c._position_tuple_from_state(p) == leg
                ]
            else:
                keys_to_finalize = list(finalize_keys or [])
            await c._finalize_closed_positions(
                keys_to_finalize,
                sym=sym,
                leg_was_flat=leg_was_flat,
                live_qty_cache=live_qty_cache,
                close_order_id=close_order_id,
                result=result,
            )

    if result["closed"] or result["stale_removed"]:
        asyncio.create_task(c.sync_live_positions())

    return result


def _open_position_keys_for_side(side: str):
    c = _core()
    """Position keys for one hedge side only (LONG or SHORT)."""
    want = str(side or "").upper()
    if want not in {"LONG", "SHORT"}:
        return []
    return [
        pk
        for pk, pos in c.state["open_positions"].items()
        if c._position_side_from_state(pos) == want
    ]


async def _dashboard_emergency_close(
    keys: list[str],
    *,
    full_leg: bool,
    log_label: str,
    empty_msg: str,
    extra: dict | None = None,
):
    c = _core()
    """Shared handler for dashboard Close All / Close All Long|Short."""
    if not keys:
        payload = {
            "status": "ok",
            "closed": 0,
            "stale_removed": 0,
            "failed": [],
            "legs": 0,
            "msg": empty_msg,
        }
        if extra:
            payload.update(extra)
        return JSONResponse(payload)

    result = await _emergency_close_batch(keys, full_leg=full_leg)
    if result.get("status") == "rate_limited":
        snap = result.get("rate_limit") or c._binance_rate_limit_snapshot()
        remaining = int(snap.get("remaining_sec") or 0)
        payload = {
            "status": "rate_limited",
            "msg": (
                f"Rate limited — stopped closing. "
                f"Auto-retry in {c.CLOSE_ALL_RETRY_SEC}s"
                + (f" (Binance backoff {remaining}s left)" if remaining else "")
            ),
            "retry_in_sec": c.CLOSE_ALL_RETRY_SEC,
            "binance_rate_limit": snap,
            "closed": int(result.get("closed") or 0),
            "stale_removed": int(result.get("stale_removed") or 0),
            "failed": result.get("failed", []),
            "failed_count": len(result.get("failed", [])),
            "legs": result.get("legs", 0),
        }
        if extra:
            payload.update(extra)
        return JSONResponse(payload, status_code=429)

    closed = int(result.get("closed") or 0)
    stale_removed = int(result.get("stale_removed") or 0)
    failed = list(result.get("failed") or [])
    print(
        f"[Emergency] {log_label}: closed {closed} + stale-removed {stale_removed} / {len(keys)} "
        f"position(s) via dashboard ({result.get('legs', 0)} leg(s))"
    )
    payload = {
        "status": "ok",
        "closed": closed,
        "stale_removed": stale_removed,
        "failed": failed,
        "failed_count": len(failed),
        "legs": result.get("legs", 0),
    }
    if extra:
        payload.update(extra)
    return JSONResponse(payload)


@app.post("/api/close-all")
async def api_close_all(_=Depends(_check_auth)):
    c = _core()
    c = _core()
    """Emergency: close all open positions immediately (one market order per hedge leg)."""
    async with c._state_lock:
        keys = list(c.state["open_positions"].keys())
    return await _dashboard_emergency_close(
        keys,
        full_leg=True,
        log_label="Close all",
        empty_msg="No open positions",
    )


@app.post("/api/close-all-long")
async def api_close_all_long(_=Depends(_check_auth)):
    c = _core()
    c = _core()
    """Emergency: close all LONG hedge legs only."""
    async with c._state_lock:
        keys = _open_position_keys_for_side("LONG")
    return await _dashboard_emergency_close(
        keys,
        full_leg=True,
        log_label="Close all LONG",
        empty_msg="No open LONG positions",
        extra={"side": "LONG"},
    )


@app.post("/api/close-all-short")
async def api_close_all_short(_=Depends(_check_auth)):
    c = _core()
    c = _core()
    """Emergency: close all SHORT hedge legs only."""
    async with c._state_lock:
        keys = _open_position_keys_for_side("SHORT")
    return await _dashboard_emergency_close(
        keys,
        full_leg=True,
        log_label="Close all SHORT",
        empty_msg="No open SHORT positions",
        extra={"side": "SHORT"},
    )


@app.post("/api/close-strategy")
async def api_close_strategy(payload: dict, _=Depends(_check_auth)):
    c = _core()
    c = _core()
    """Emergency: close all open positions for one strategy/tab."""
    tab = str(payload.get("tab") or "").strip()
    allowed_tabs = set(c.TABS) | {"SafeGuard", "Recovered"}
    if tab not in allowed_tabs:
        return JSONResponse({"status": "error", "msg": f"Unknown strategy: {tab}"}, status_code=400)

    async with c._state_lock:
        keys = [
            pos_key
            for pos_key, pos in c.state["open_positions"].items()
            if pos.get("tab") == tab
        ]
    if not keys:
        return JSONResponse({"status": "ok", "closed": 0, "stale_removed": 0, "failed": [], "legs": 0, "msg": f"No open positions for {tab}"})

    result = await _emergency_close_batch(keys, full_leg=False)
    if result.get("status") == "rate_limited":
        snap = result.get("rate_limit") or c._binance_rate_limit_snapshot()
        remaining = int(snap.get("remaining_sec") or 0)
        return JSONResponse({
            "status": "rate_limited",
            "msg": (
                f"Rate limited — stopped closing. "
                f"Auto-retry in {c.CLOSE_ALL_RETRY_SEC}s"
                + (f" (Binance backoff {remaining}s left)" if remaining else "")
            ),
            "retry_in_sec": c.CLOSE_ALL_RETRY_SEC,
            "binance_rate_limit": snap,
            "tab": tab,
            "closed": int(result.get("closed") or 0),
            "stale_removed": int(result.get("stale_removed") or 0),
            "failed": result.get("failed", []),
            "failed_count": len(result.get("failed", [])),
            "legs": result.get("legs", 0),
        }, status_code=429)

    closed = int(result.get("closed") or 0)
    stale_removed = int(result.get("stale_removed") or 0)
    failed = list(result.get("failed") or [])
    print(
        f"[Emergency] Closed {closed} + stale-removed {stale_removed}/{len(keys)} "
        f"{tab} position(s) via dashboard ({result.get('legs', 0)} leg(s))"
    )
    return JSONResponse({
        "status": "ok",
        "tab": tab,
        "closed": closed,
        "stale_removed": stale_removed,
        "failed": failed,
        "failed_count": len(failed),
        "legs": result.get("legs", 0),
    })


@app.post("/api/test-hedge")
async def api_test_hedge(_=Depends(_check_auth), __=Depends(_require_testnet)):
    c = _core()
    c = _core()
    """
    Hedge Mode full test:
    1. Open LONG + SHORT on BTCUSDT simultaneously (simulating 2 strategies)
    2. Place separate SL/TP for each leg
    3. Close LONG leg only (cancel its SL/TP, market SELL positionSide=LONG)
    4. Verify SHORT leg SL/TP still alive
    5. Close SHORT leg
    """
    if not c.LIVE_MODE:
        return JSONResponse({"error": "c.LIVE_MODE is off"}, status_code=400)

    sym = "BTCUSDT"
    log: list[str] = []

    async def _get_price() -> float:
        r = await c._http_client.get(binance_live.BASE_URL + "/fapi/v1/ticker/price",
                                   params={"symbol": sym})
        r.raise_for_status()
        return float(r.json()["price"])

    async def _open_leg(side: str, pos_side: str, qty: float, price: float) -> dict:
        entry_side = "BUY" if side == "Long" else "SELL"
        res = await binance_live.place_market_order(
            c._http_client, sym, entry_side, qty, position_side=pos_side
        )
        avg_px = float(res.get("avgPrice") or 0)
        fp = avg_px if avg_px > 0 else price
        close_side = "SELL" if side == "Long" else "BUY"
        sl_px = binance_live.round_price(sym, fp * (0.97 if side == "Long" else 1.03))
        tp_px = binance_live.round_price(sym, fp * (1.05 if side == "Long" else 0.95))
        sl_res = await binance_live.place_stop_loss(
            c._http_client, sym, close_side, sl_px, qty, position_side=pos_side
        )
        tp_res = await binance_live.place_take_profit(
            c._http_client, sym, close_side, tp_px, qty, position_side=pos_side
        )
        sl_id = sl_res.get("algoId") or sl_res.get("orderId")
        tp_id = tp_res.get("algoId") or tp_res.get("orderId")
        log.append(f"  {side} opened @ {fp:.2f}  SL={sl_id}@{sl_px}  TP={tp_id}@{tp_px}")
        return {"pos_side": pos_side, "close_side": close_side,
                "qty": qty, "fill": fp, "sl_id": sl_id, "tp_id": tp_id}

    async def _close_leg(leg: dict, tag: str):
        sl_id, tp_id = leg["sl_id"], leg["tp_id"]
        # Cancel this leg's SL/TP only
        for label, oid in [("SL", sl_id), ("TP", tp_id)]:
            if oid:
                try:
                    await binance_live.cancel_algo_order(c._http_client, algo_id=oid)
                    log.append(f"  {tag} {label} cancelled id={oid}")
                except Exception as e:
                    log.append(f"  {tag} {label} cancel WARN: {e}")
        # Market close
        res = await binance_live.place_market_order(
            c._http_client, sym, leg["close_side"], leg["qty"], position_side=leg["pos_side"]
        )
        close_px = float(res.get("avgPrice") or 0) or leg["fill"]
        pnl = (close_px - leg["fill"]) * leg["qty"] * (1 if leg["pos_side"] == "LONG" else -1)
        log.append(f"  {tag} closed orderId={res.get('orderId')} @ {close_px:.2f}  PnL≈${pnl:.4f}")

    async def _list_algo_orders() -> list:
        try:
            r = await c._http_client.get(binance_live.BASE_URL + "/fapi/v1/openAlgoOrders",
                                       params={"symbol": sym})
            r.raise_for_status()
            return r.json().get("orders", [])
        except Exception:
            return []

    try:
        await binance_live.fetch_exchange_info(c._http_client)
        await binance_live.set_margin_type(c._http_client, sym, "ISOLATED")
        await c._ensure_symbol_leverage(sym)
        price = await _get_price()
        qty = max(binance_live.round_qty(sym, 50.0 / price),
                  binance_live.symbol_info.get(sym, {}).get("min_qty", 0.001))
        log.append(f"price={price:.2f}  qty={qty}")

        # ── Step 1: Open both legs ──────────────────────────────────────────
        log.append("=== Step 1: Open LONG + SHORT ===")
        long_leg  = await _open_leg("Long",  "LONG",  qty, price)
        short_leg = await _open_leg("Short", "SHORT", qty, price)

        # ── Step 2: Verify algo orders ──────────────────────────────────────
        algo_orders = await _list_algo_orders()
        log.append(f"=== Step 2: Open algo orders = {len(algo_orders)} (expect 4) ===")
        for o in algo_orders:
            log.append(f"  algoId={o.get('algoId')} type={o.get('type')} side={o.get('side')} "
                       f"positionSide={o.get('positionSide')} triggerPrice={o.get('triggerPrice')}")

        # ── Step 3: Close LONG leg only ─────────────────────────────────────
        log.append("=== Step 3: Close LONG leg ===")
        await _close_leg(long_leg, "LONG")

        # ── Step 4: Verify SHORT leg SL/TP still alive ──────────────────────
        algo_orders2 = await _list_algo_orders()
        log.append(f"=== Step 4: Remaining algo orders = {len(algo_orders2)} (expect 2) ===")
        for o in algo_orders2:
            log.append(f"  algoId={o.get('algoId')} positionSide={o.get('positionSide')} "
                       f"type={o.get('type')} triggerPrice={o.get('triggerPrice')}")

        # ── Step 5: Close SHORT leg ─────────────────────────────────────────
        log.append("=== Step 5: Close SHORT leg ===")
        await _close_leg(short_leg, "SHORT")

        # ── Step 6: Final verify ────────────────────────────────────────────
        algo_orders3 = await _list_algo_orders()
        log.append(f"=== Step 6: Final algo orders = {len(algo_orders3)} (expect 0) ===")
        positions = await binance_live.get_position_risk(c._http_client, sym)
        for p in positions:
            amt = float(p.get("positionAmt", 0))
            log.append(f"  {p['positionSide']} positionAmt={amt}")

        return JSONResponse({"status": "ok", "steps": log})

    except Exception as e:
        log.append(f"ERROR: {e}")
        return JSONResponse({"status": "error", "steps": log, "error": str(e)}, status_code=500)


@app.post("/api/test-hedge-same-side")
async def api_test_hedge_same_side(_=Depends(_check_auth), __=Depends(_require_testnet)):
    c = _core()
    c = _core()
    """
    Test: 2 strategies open BTCUSDT LONG simultaneously.
    Close them one at a time and verify each close is independent.
    """
    if not c.LIVE_MODE:
        return JSONResponse({"error": "c.LIVE_MODE is off"}, status_code=400)

    sym = "BTCUSDT"
    log: list[str] = []

    async def _price() -> float:
        r = await c._http_client.get(binance_live.BASE_URL + "/fapi/v1/ticker/price",
                                   params={"symbol": sym})
        r.raise_for_status()
        return float(r.json()["price"])

    try:
        await binance_live.fetch_exchange_info(c._http_client)
        await binance_live.set_margin_type(c._http_client, sym, "ISOLATED")
        await c._ensure_symbol_leverage(sym)
        price = await _price()
        qty = max(binance_live.round_qty(sym, 50.0 / price),
                  binance_live.symbol_info.get(sym, {}).get("min_qty", 0.001))

        log.append(f"price={price:.2f}  qty={qty}")

        # ── Open LONG #1 (Strategy A) ───────────────────────────────────────
        res1 = await binance_live.place_market_order(c._http_client, sym, "BUY", qty, position_side="LONG")
        fp1 = float(res1.get("avgPrice") or 0) or price
        sl1_res = await binance_live.place_stop_loss(c._http_client, sym, "SELL", round(fp1*0.97,1), qty, position_side="LONG")
        tp1_res = await binance_live.place_take_profit(c._http_client, sym, "SELL", round(fp1*1.05,1), qty, position_side="LONG")
        sl1 = sl1_res.get("algoId"); tp1 = tp1_res.get("algoId")
        log.append(f"StratA LONG opened @ {fp1:.2f}  SL={sl1}  TP={tp1}")

        # ── Open LONG #2 (Strategy B) ───────────────────────────────────────
        res2 = await binance_live.place_market_order(c._http_client, sym, "BUY", qty, position_side="LONG")
        fp2 = float(res2.get("avgPrice") or 0) or price
        sl2_res = await binance_live.place_stop_loss(c._http_client, sym, "SELL", round(fp2*0.97,1), qty, position_side="LONG")
        tp2_res = await binance_live.place_take_profit(c._http_client, sym, "SELL", round(fp2*1.05,1), qty, position_side="LONG")
        sl2 = sl2_res.get("algoId"); tp2 = tp2_res.get("algoId")
        log.append(f"StratB LONG opened @ {fp2:.2f}  SL={sl2}  TP={tp2}")

        # Verify exchange sees combined qty
        pos = await binance_live.get_position_risk(c._http_client, sym)
        for p in pos:
            if float(p["positionAmt"]) != 0:
                log.append(f"Exchange LONG positionAmt={p['positionAmt']} (expect {qty*2:.4f})")

        # ── Close Strategy A only (cancel its SL/TP, market sell qty only) ─
        log.append("--- Close StratA only ---")
        await binance_live.cancel_algo_order(c._http_client, algo_id=sl1)
        log.append(f"StratA SL cancelled {sl1}")
        await binance_live.cancel_algo_order(c._http_client, algo_id=tp1)
        log.append(f"StratA TP cancelled {tp1}")
        close1 = await binance_live.place_market_order(c._http_client, sym, "SELL", qty, position_side="LONG")
        log.append(f"StratA SELL qty={qty} orderId={close1.get('orderId')}")

        # Verify StratB's SL/TP still alive
        log.append("--- Verify StratB SL/TP alive ---")
        open_ids = await binance_live.fetch_open_algo_order_ids(c._http_client, sym)
        sl2_alive = int(sl2) in open_ids if sl2 else None
        tp2_alive = int(tp2) in open_ids if tp2 else None
        # Verify exchange qty reduced
        pos2 = await binance_live.get_position_risk(c._http_client, sym)
        for p in pos2:
            if p["positionSide"] == "LONG":
                log.append(f"Exchange LONG after StratA close: positionAmt={p['positionAmt']} (expect {qty:.4f})")
        log.append(f"StratB SL={sl2} alive={sl2_alive}  TP={tp2} alive={tp2_alive}")
        log.append("(Note: open_ids may return empty on testnet — cancel test below confirms)")

        # ── Close Strategy B ────────────────────────────────────────────────
        log.append("--- Close StratB ---")
        if sl2:
            r = await binance_live.cancel_algo_order(c._http_client, algo_id=sl2)
            log.append(f"StratB SL cancel result: {'ok' if r is not None else 'not found'}")
        if tp2:
            r = await binance_live.cancel_algo_order(c._http_client, algo_id=tp2)
            log.append(f"StratB TP cancel result: {'ok' if r is not None else 'not found'}")
        close2 = await binance_live.place_market_order(c._http_client, sym, "SELL", qty, position_side="LONG")
        log.append(f"StratB SELL qty={qty} orderId={close2.get('orderId')}")

        # Final verify
        pos3 = await binance_live.get_position_risk(c._http_client, sym)
        for p in pos3:
            log.append(f"Final {p['positionSide']} positionAmt={p['positionAmt']}")

        return JSONResponse({"status": "ok", "steps": log})

    except Exception as e:
        log.append(f"ERROR: {e}")
        return JSONResponse({"status": "error", "steps": log, "error": str(e)}, status_code=500)


@app.post("/api/test-full-hedge")
async def api_test_full_hedge(_=Depends(_check_auth), __=Depends(_require_testnet)):
    c = _core()
    c = _core()
    """
    Full multi-strategy hedge test on BTCUSDT:
      • 3 LONG positions  (simulating Tab1, Tab2, Tab3)
      • 3 SHORT positions (simulating Tab4, Tab5, Tab6)
    Then close each leg individually and verify exchange c.state after every close.
    """
    if not c.LIVE_MODE:
        return JSONResponse({"error": "c.LIVE_MODE is off"}, status_code=400)

    sym = "BTCUSDT"
    log: list[str] = []
    legs: list[dict] = []   # {label, pos_side, close_side, qty, fill, sl_id, tp_id}

    # ── helpers ──────────────────────────────────────────────────────────────
    async def _price() -> float:
        r = await c._http_client.get(binance_live.BASE_URL + "/fapi/v1/ticker/price",
                                   params={"symbol": sym})
        r.raise_for_status()
        return float(r.json()["price"])

    async def _exchange_snapshot() -> dict:
        """Return {positionSide: amt} and list of open algo order IDs."""
        pos = await binance_live.get_position_risk(c._http_client, sym)
        pos_map = {p["positionSide"]: float(p["positionAmt"]) for p in pos}
        algo_ids = await binance_live.fetch_open_algo_order_ids(c._http_client, sym)
        return {"positions": pos_map, "algo_ids": algo_ids}

    async def _open_leg(label: str, side: str, qty: float, price: float) -> dict:
        pos_side  = "LONG"  if side == "Long"  else "SHORT"
        entry_side = "BUY"  if side == "Long"  else "SELL"
        close_side = "SELL" if side == "Long"  else "BUY"

        res = await binance_live.place_market_order(
            c._http_client, sym, entry_side, qty, position_side=pos_side)
        fp = float(res.get("avgPrice") or 0) or price

        sl_px = round(fp * (0.97 if side == "Long" else 1.03), 1)
        tp_px = round(fp * (1.05 if side == "Long" else 0.95), 1)

        sl_res = await binance_live.place_stop_loss(
            c._http_client, sym, close_side, sl_px, qty, position_side=pos_side)
        tp_res = await binance_live.place_take_profit(
            c._http_client, sym, close_side, tp_px, qty, position_side=pos_side)

        sl_id = sl_res.get("algoId")
        tp_id = tp_res.get("algoId")
        log.append(f"  [{label}] {side} @ {fp:.2f}  qty={qty}  SL={sl_id}@{sl_px}  TP={tp_id}@{tp_px}")
        return {"label": label, "side": side, "pos_side": pos_side,
                "close_side": close_side, "qty": qty, "fill": fp,
                "sl_id": sl_id, "tp_id": tp_id}

    async def _close_leg(leg: dict):
        # 1. Cancel SL/TP for THIS leg only
        for name, oid in [("SL", leg["sl_id"]), ("TP", leg["tp_id"])]:
            if oid:
                try:
                    await binance_live.cancel_algo_order(c._http_client, algo_id=oid)
                    log.append(f"    cancel {name} id={oid} → ok")
                except Exception as e:
                    log.append(f"    cancel {name} id={oid} → WARN: {e}")
        # 2. Market close this leg's qty only
        close_res = await binance_live.place_market_order(
            c._http_client, sym, leg["close_side"], leg["qty"], position_side=leg["pos_side"])
        close_px = float(close_res.get("avgPrice") or 0) or leg["fill"]
        mult = 1 if leg["pos_side"] == "LONG" else -1
        pnl = (close_px - leg["fill"]) * leg["qty"] * mult
        log.append(f"    market close orderId={close_res.get('orderId')} @ {close_px:.2f}  PnL≈${pnl:.4f}")

    def _snapshot_line(snap: dict, remaining_legs: list) -> str:
        pm = snap["positions"]
        longs_ex  = pm.get("LONG",  0.0)
        shorts_ex = pm.get("SHORT", 0.0)
        algo_cnt  = len(snap["algo_ids"])
        expect_algo = len(remaining_legs) * 2   # SL+TP per remaining leg
        state_desc = ", ".join(f"[{l['label']}]{l['side'][0]}" for l in remaining_legs) or "none"
        return (f"  Exchange: LONG={longs_ex:.4f}  SHORT={shorts_ex:.4f}  "
                f"algoOrders={algo_cnt}(expect {expect_algo})  "
                f"remaining={state_desc}")

    # ── main flow ─────────────────────────────────────────────────────────────
    try:
        await binance_live.fetch_exchange_info(c._http_client)
        await binance_live.set_margin_type(c._http_client, sym, "ISOLATED")
        await c._ensure_symbol_leverage(sym)
        price = await _price()
        qty = max(binance_live.round_qty(sym, 50.0 / price),
                  binance_live.symbol_info.get(sym, {}).get("min_qty", 0.001))
        log.append(f"price={price:.2f}  qty={qty} (~$50 per leg)")

        # ═══════════════════════════════════════════════════════
        log.append("═" * 50)
        log.append("PHASE 1: Open 3 LONG + 3 SHORT")
        log.append("═" * 50)

        for label in ["Tab1", "Tab2", "Tab3"]:
            leg = await _open_leg(label, "Long", qty, price)
            legs.append(leg)
        for label in ["Tab4", "Tab5", "Tab6"]:
            leg = await _open_leg(label, "Short", qty, price)
            legs.append(leg)

        snap = await _exchange_snapshot()
        log.append("After opening all 6 legs:")
        log.append(_snapshot_line(snap, legs))

        # ═══════════════════════════════════════════════════════
        log.append("═" * 50)
        log.append("PHASE 2: Close each leg one-by-one")
        log.append("═" * 50)

        for i, leg in enumerate(legs):
            remaining_before = legs[i+1:]
            log.append(f"── Close [{leg['label']}] {leg['side']} (leg {i+1}/6) ──")
            await _close_leg(leg)
            snap = await _exchange_snapshot()
            log.append(_snapshot_line(snap, remaining_before))
            # Verify: remaining legs' algoIds still alive
            if remaining_before:
                remaining_ids = set()
                for rl in remaining_before:
                    if rl["sl_id"]: remaining_ids.add(int(rl["sl_id"]))
                    if rl["tp_id"]: remaining_ids.add(int(rl["tp_id"]))
                missing = remaining_ids - snap["algo_ids"]
                if missing:
                    log.append(f"  ⚠ Missing algoIds for remaining legs: {missing}")
                else:
                    log.append(f"  ✓ All remaining legs' algoIds still alive (or not yet visible via API)")

        # ═══════════════════════════════════════════════════════
        log.append("═" * 50)
        log.append("PHASE 3: Final verification")
        log.append("═" * 50)
        snap = await _exchange_snapshot()
        pm = snap["positions"]
        log.append(f"  LONG  positionAmt = {pm.get('LONG', 0.0):.4f}  (expect 0)")
        log.append(f"  SHORT positionAmt = {pm.get('SHORT', 0.0):.4f}  (expect 0)")
        log.append(f"  Open algoOrders   = {len(snap['algo_ids'])}          (expect 0)")
        all_clear = (abs(pm.get("LONG", 0.0)) < 1e-6
                     and abs(pm.get("SHORT", 0.0)) < 1e-6
                     and len(snap["algo_ids"]) == 0)
        log.append("✅ ALL CLEAR" if all_clear else "❌ POSITIONS OR ORDERS REMAINING — CHECK MANUALLY")

        return JSONResponse({"status": "ok", "steps": log})

    except Exception as e:
        log.append(f"ERROR: {e}")
        # Cleanup attempt
        try:
            pos = await binance_live.get_position_risk(c._http_client, sym)
            for p in pos:
                amt = float(p.get("positionAmt", 0))
                if abs(amt) > 1e-6:
                    s = "SELL" if amt > 0 else "BUY"
                    ps = "LONG" if amt > 0 else "SHORT"
                    await binance_live.place_market_order(c._http_client, sym, s, abs(amt), position_side=ps)
                    log.append(f"[cleanup] closed {ps} {abs(amt)}")
        except Exception as ce:
            log.append(f"[cleanup failed] {ce}")
        return JSONResponse({"status": "error", "steps": log, "error": str(e)}, status_code=500)


@app.get("/api/logs")
async def api_logs(_=Depends(_check_auth)):
    c = _core()
    c = _core()
    """Return the last 100 captured log lines."""
    return JSONResponse({"logs": list(c._log_buffer)[-100:]})


@app.get("/api/health")
async def api_health(_=Depends(_check_auth)):
    c = _core()
    c = _core()
    """Return lightweight bot health without making fresh exchange API calls."""
    return JSONResponse(c._health_snapshot())


_INSTANCE_LOCK_HANDLE = None
_INSTANCE_MUTEX_HANDLE = None
_INSTANCE_MUTEX_ALREADY_EXISTS = 183
_INSTANCE_MUTEX_NAME = "Local\\AntigravityMultiStrategyServer"
