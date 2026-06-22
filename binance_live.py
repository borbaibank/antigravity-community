"""
binance_live.py — Binance USDT-M Futures live order execution helpers.

All functions are async and accept the shared httpx.AsyncClient from server.py.
Import only when LIVE_MODE=True; paper trading never touches this module.

Exchange endpoints:
  Live:    https://fapi.binance.com
  Testnet: https://testnet.binancefuture.com

User Data Stream WS (private routed path):
  Live:    wss://fstream.binance.com/private/ws/<listenKey>
  Testnet: wss://stream.binancefuture.com/private/ws/<listenKey>
"""

import hashlib
import hmac
import math
import time
from urllib.parse import urlencode

# Signed-request clock: offset = Binance serverTime - local midpoint (ms).
_time_offset_ms = 0
_last_time_sync_mono = 0.0
RECV_WINDOW = 10000
TIME_SYNC_INTERVAL_SEC = 600

import httpx

from config import ALGO_WORKING_TYPE, BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_TESTNET

BASE_URL = (
    "https://testnet.binancefuture.com" if BINANCE_TESTNET
    else "https://fapi.binance.com"
)
WS_BASE = (
    "wss://stream.binancefuture.com" if BINANCE_TESTNET
    else "wss://fstream.binance.com"
)

# Populated on startup by fetch_exchange_info()
# { "BTCUSDT": {"qty_precision": 3, "price_precision": 2, "min_qty": 0.001} }
symbol_info: dict = {}
PROTECTIVE_ORDER_TYPES = {"STOP_MARKET", "TAKE_PROFIT_MARKET", "TAKE_PROFIT"}


# ── Signing ───────────────────────────────────────────────────────────────────

def _sign(params: dict) -> str:
    query = urlencode(params)
    return hmac.new(
        BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256
    ).hexdigest()


def _headers() -> dict:
    return {"X-MBX-APIKEY": BINANCE_API_KEY}


def _timestamp_ms() -> int:
    return int(time.time() * 1000) + _time_offset_ms


def time_offset_ms() -> int:
    """Current Binance server time minus local wall clock (ms)."""
    return _time_offset_ms


async def sync_server_time(client: httpx.AsyncClient, force: bool = False) -> int:
    """Align signed-request timestamps with Binance /fapi/v1/time."""
    global _time_offset_ms, _last_time_sync_mono
    now_mono = time.monotonic()
    if (
        not force
        and _last_time_sync_mono
        and (now_mono - _last_time_sync_mono) < TIME_SYNC_INTERVAL_SEC
    ):
        return _time_offset_ms
    t0 = time.time()
    r = await client.get(BASE_URL + "/fapi/v1/time")
    r.raise_for_status()
    server_ms = int(r.json()["serverTime"])
    t1 = time.time()
    local_ms = int(((t0 + t1) / 2) * 1000)
    _time_offset_ms = server_ms - local_ms
    _last_time_sync_mono = time.monotonic()
    drift = abs(_time_offset_ms)
    if drift >= 500 or force:
        sign = "+" if _time_offset_ms >= 0 else ""
        print(f"[Live] Binance time offset synced: {sign}{_time_offset_ms}ms (drift {drift}ms)")
    return _time_offset_ms


# ── Signed request helpers ────────────────────────────────────────────────────

async def _sreq(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    params: dict = None,
    *,
    _retry_on_clock: bool = True,
):
    await sync_server_time(client)
    p = dict(params or {})
    p["timestamp"] = _timestamp_ms()
    p["recvWindow"] = RECV_WINDOW
    p["signature"] = _sign(p)
    url = BASE_URL + path
    h = _headers()
    try:
        if method == "GET":
            r = await client.get(url, params=p, headers=h)
        elif method == "POST":
            r = await client.post(url, params=p, headers=h)
        elif method == "DELETE":
            r = await client.delete(url, params=p, headers=h)
        elif method == "PUT":
            r = await client.put(url, params=p, headers=h)
        else:
            raise ValueError(f"Unknown method: {method}")
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        body = e.response.text if e.response is not None else ""
        if "-1021" in body and _retry_on_clock:
            print("[WARN] Binance -1021 timestamp mismatch — resyncing server time and retrying once")
            await sync_server_time(client, force=True)
            return await _sreq(
                client, method, path, params, _retry_on_clock=False,
            )
        if "-1021" in body:
            print(
                "[ERROR] Binance timestamp mismatch (-1021) after resync — "
                "check that system clock is accurate"
            )
        raise
    return r.json()


# ── Exchange info & precision ─────────────────────────────────────────────────

async def fetch_exchange_info(client: httpx.AsyncClient):
    """Fetch symbol precision + filter data. Call once on startup. Retries 3x."""
    global symbol_info
    import asyncio as _asyncio
    last_err = None
    for attempt in range(3):
        try:
            r = await client.get(BASE_URL + "/fapi/v1/exchangeInfo")
            r.raise_for_status()
            break
        except Exception as e:
            last_err = e
            if attempt < 2:
                await _asyncio.sleep(2 ** attempt)
    else:
        print(f"[WARN] fetch_exchange_info failed after 3 attempts: {last_err} — precision defaults used")
        return
    data = r.json()
    for s in data.get("symbols", []):
        sym = s["symbol"]
        qty_prec   = s.get("quantityPrecision", 3)
        price_prec = s.get("pricePrecision", 2)
        min_qty = 0.0
        min_notional = 0.0
        tick_size = 0.0
        for f in s.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                min_qty = float(f["minQty"])
            elif f["filterType"] == "PRICE_FILTER":
                tick_size = float(f.get("tickSize") or 0)
            elif f["filterType"] in {"MIN_NOTIONAL", "NOTIONAL"}:
                min_notional = float(f.get("notional") or f.get("minNotional") or 0)
        symbol_info[sym] = {
            "qty_precision":   qty_prec,
            "price_precision": price_prec,
            "tick_size":       tick_size,
            "min_qty":         min_qty,
            "min_notional":    min_notional,
        }
    print(f"[Live] Exchange info loaded: {len(symbol_info)} symbols")


def round_qty(sym: str, qty: float) -> float:
    prec = symbol_info.get(sym, {}).get("qty_precision", 3)
    return round(qty, prec)


def _tick_size(sym: str) -> float:
    try:
        return float(symbol_info.get(sym, {}).get("tick_size") or 0)
    except (TypeError, ValueError):
        return 0.0


def _price_decimals(sym: str, tick: float | None = None) -> int:
    tick = tick if tick is not None else _tick_size(sym)
    if tick > 0:
        s = f"{tick:.16f}".rstrip("0")
        if "." in s:
            return len(s.split(".", 1)[1])
    return int(symbol_info.get(sym, {}).get("price_precision", 2))


def round_price_tick(sym: str, price: float, *, mode: str = "nearest") -> float:
    """Round price to Binance PRICE_FILTER tickSize (fixes -4014 on algo TP limit)."""
    tick = _tick_size(sym)
    if tick <= 0:
        prec = symbol_info.get(sym, {}).get("price_precision", 2)
        return round(float(price), prec)
    p = float(price)
    steps = p / tick
    if mode == "up":
        n = math.ceil(steps - 1e-12)
    elif mode == "down":
        n = math.floor(steps + 1e-12)
    else:
        n = round(steps)
    dec = _price_decimals(sym, tick)
    return round(n * tick, dec)


def round_price(sym: str, price: float) -> float:
    return round_price_tick(sym, price, mode="nearest")


def format_price(sym: str, price: float) -> str:
    """Fixed-decimal string — avoids scientific notation on tiny-price coins
    (Binance -1102 rejects 'triggerPrice' like '9.5399e-05')."""
    tick = _tick_size(sym)
    rounded = round_price_tick(sym, price) if tick > 0 else round(
        float(price), symbol_info.get(sym, {}).get("price_precision", 2)
    )
    dec = _price_decimals(sym, tick)
    return f"{rounded:.{dec}f}"


def is_protective_order(order: dict) -> bool:
    """Return True for SL/TP style futures orders managed by the bot."""
    order_type = str(order.get("type") or order.get("orderType") or "").upper()
    return order_type in PROTECTIVE_ORDER_TYPES


def order_matches_position_side(order: dict, position_side: str = None) -> bool:
    """Filter helper for hedge-mode LONG/SHORT order ownership."""
    if not position_side:
        return True
    return str(order.get("positionSide", "")).upper() == str(position_side).upper()


# ── Account setup ─────────────────────────────────────────────────────────────

async def set_margin_type(client: httpx.AsyncClient, symbol: str, margin_type: str = "ISOLATED"):
    """Set margin type — best-effort. Never raises to caller; failures here
    must not block entry (Binance rejects with 400 when symbol has open
    position/orders). Ignores -4046 silently (already set)."""
    try:
        return await _sreq(client, "POST", "/fapi/v1/marginType", {
            "symbol": symbol, "marginType": margin_type,
        })
    except httpx.HTTPStatusError as e:
        body = e.response.text if e.response is not None else ""
        if "-4046" in body:
            return {}  # already set — fine
        # Any other 400: log and move on. Margin type is an optimization, not a prereq.
        print(f"[margin_type] {symbol} {margin_type} best-effort skip: {body[:120]}")
        return {}
    except Exception as e:
        print(f"[margin_type] {symbol} {margin_type} best-effort skip: {e}")
        return {}


async def set_leverage(client: httpx.AsyncClient, symbol: str, leverage: int):
    return await _sreq(client, "POST", "/fapi/v1/leverage", {
        "symbol": symbol, "leverage": leverage,
    })


# ── Order placement ───────────────────────────────────────────────────────────

async def place_market_order(
    client: httpx.AsyncClient, symbol: str, side: str, quantity: float,
    position_side: str = "LONG", reduce_only: bool = False,
    client_order_id: str = None
) -> dict:
    """Market entry/exit (Hedge Mode). side: 'BUY'/'SELL', position_side: 'LONG'/'SHORT'."""
    params = {
        "symbol":       symbol,
        "side":         side,
        "type":         "MARKET",
        "quantity":     quantity,
        "positionSide": position_side,
    }
    if reduce_only:
        params["reduceOnly"] = "true"
    if client_order_id:
        params["newClientOrderId"] = client_order_id
    return await _sreq(client, "POST", "/fapi/v1/order", params)


async def place_limit_entry(
    client: httpx.AsyncClient, symbol: str, side: str, price: float, quantity: float,
    position_side: str = "LONG", time_in_force: str = "GTC",
    client_order_id: str = None,
) -> dict:
    """Limit entry (Hedge Mode). side: BUY/SELL, position_side: LONG/SHORT."""
    params = {
        "symbol":       symbol,
        "side":         side,
        "type":         "LIMIT",
        "timeInForce":  time_in_force,
        "quantity":     round_qty(symbol, quantity),
        "price":        format_price(symbol, price),
        "positionSide": position_side,
    }
    if client_order_id:
        params["newClientOrderId"] = client_order_id
    return await _sreq(client, "POST", "/fapi/v1/order", params)


async def place_price_match_ioc_order(
    client: httpx.AsyncClient, symbol: str, side: str, quantity: float,
    position_side: str = "LONG", price_match: str = "OPPONENT",
    client_order_id: str = None
) -> dict:
    """IOC limit close using Binance priceMatch, useful when testnet rejects MARKET by percent filter."""
    params = {
        "symbol":       symbol,
        "side":         side,
        "type":         "LIMIT",
        "timeInForce":  "IOC",
        "quantity":     quantity,
        "positionSide": position_side,
        "priceMatch":   price_match,
    }
    if client_order_id:
        params["newClientOrderId"] = client_order_id
    return await _sreq(client, "POST", "/fapi/v1/order", params)


async def test_market_order(
    client: httpx.AsyncClient, symbol: str, side: str, quantity: float,
    position_side: str = "LONG"
) -> dict:
    """Validate a market order (test endpoint, no fill)."""
    params = {
        "symbol":       symbol,
        "side":         side,
        "type":         "MARKET",
        "quantity":     quantity,
        "positionSide": position_side,
    }
    return await _sreq(client, "POST", "/fapi/v1/order/test", params)


async def place_stop_loss(
    client: httpx.AsyncClient, symbol: str, side: str, stop_price: float,
    quantity: float = None, position_side: str = "LONG",
    client_algo_id: str = None
) -> dict:
    """Algo CONDITIONAL STOP order (Hedge Mode). Returns {"algoId": ...}.
    side: 'SELL' for Long position, 'BUY' for Short position.
    """
    params = {
        "symbol":       symbol,
        "algoType":     "CONDITIONAL",
        "side":         side,
        "positionSide": position_side,
        "type":         "STOP_MARKET",
        "triggerPrice": format_price(symbol, stop_price),
        "workingType":  ALGO_WORKING_TYPE,
    }
    if quantity is not None:
        params["quantity"] = round_qty(symbol, quantity)
    if client_algo_id:
        params["clientAlgoId"] = client_algo_id
    return await _sreq(client, "POST", "/fapi/v1/algoOrder", params)


async def place_take_profit(
    client: httpx.AsyncClient, symbol: str, side: str, stop_price: float,
    quantity: float = None, position_side: str = "LONG",
    client_algo_id: str = None
) -> dict:
    """Algo CONDITIONAL TAKE_PROFIT order (Hedge Mode). Returns {"algoId": ...}.
    side: 'SELL' for Long position, 'BUY' for Short position.
    """
    params = {
        "symbol":       symbol,
        "algoType":     "CONDITIONAL",
        "side":         side,
        "positionSide": position_side,
        "type":         "TAKE_PROFIT_MARKET",
        "triggerPrice": format_price(symbol, stop_price),
        "workingType":  ALGO_WORKING_TYPE,
    }
    if quantity is not None:
        params["quantity"] = round_qty(symbol, quantity)
    if client_algo_id:
        params["clientAlgoId"] = client_algo_id
    return await _sreq(client, "POST", "/fapi/v1/algoOrder", params)


async def place_take_profit_limit(
    client: httpx.AsyncClient, symbol: str, side: str, trigger_price: float,
    quantity: float = None, position_side: str = "LONG",
    limit_price: float | None = None,
    client_algo_id: str = None,
) -> dict:
    """Algo CONDITIONAL TAKE_PROFIT limit order (Hedge Mode)."""
    px = limit_price if limit_price is not None else trigger_price
    params = {
        "symbol":       symbol,
        "algoType":     "CONDITIONAL",
        "side":         side,
        "positionSide": position_side,
        "type":         "TAKE_PROFIT",
        "triggerPrice": format_price(symbol, trigger_price),
        "price":        format_price(symbol, px),
        "workingType":  ALGO_WORKING_TYPE,
    }
    if quantity is not None:
        params["quantity"] = round_qty(symbol, quantity)
    if client_algo_id:
        params["clientAlgoId"] = client_algo_id
    return await _sreq(client, "POST", "/fapi/v1/algoOrder", params)


async def place_exchange_take_profit(
    client: httpx.AsyncClient, symbol: str, side: str, tp_price: float,
    quantity: float, position_side: str = "LONG",
    *, tp_style: str = "market", client_algo_id: str = None,
) -> dict:
    """Place TP on exchange — STOP_MARKET SL is separate; tp_style market|limit."""
    if str(tp_style).lower() == "limit":
        try:
            return await place_take_profit_limit(
                client, symbol, side, tp_price, quantity,
                position_side=position_side, limit_price=tp_price,
                client_algo_id=client_algo_id,
            )
        except httpx.HTTPStatusError as e:
            body = e.response.text if e.response is not None else ""
            if "-4014" not in body:
                raise
            print(
                f"[Live] TP limit tick reject on {symbol}; "
                f"fallback to TAKE_PROFIT_MARKET @ {format_price(symbol, tp_price)}"
            )
    return await place_take_profit(
        client, symbol, side, tp_price, quantity,
        position_side=position_side, client_algo_id=client_algo_id,
    )


# ── Order management ──────────────────────────────────────────────────────────

async def cancel_order(client: httpx.AsyncClient, symbol: str, order_id: int) -> dict:
    """Cancel a specific order. Silently ignores -2011 (already filled/cancelled)."""
    try:
        return await _sreq(client, "DELETE", "/fapi/v1/order", {
            "symbol": symbol, "orderId": order_id,
        })
    except httpx.HTTPStatusError as e:
        if "-2011" in e.response.text or "Unknown order" in e.response.text:
            return {}
        raise


async def cancel_algo_order(client: httpx.AsyncClient, algo_id: int = None, client_algo_id: str = None) -> dict:
    """Cancel an algo (CONDITIONAL) SL or TP order via the algo order endpoint."""
    params = {}
    if algo_id:
        params["algoId"] = int(algo_id)
    if client_algo_id:
        params["clientAlgoId"] = client_algo_id
    if not params:
        return {}
    try:
        return await _sreq(client, "DELETE", "/fapi/v1/algoOrder", params)
    except httpx.HTTPStatusError as e:
        text = e.response.text
        if "-2011" in text or "Unknown order" in text or "not exist" in text or "not found" in text:
            return {}
        raise


async def cancel_all_orders(
    client: httpx.AsyncClient,
    symbol: str,
    position_side: str = None,
    protective_only: bool = False,
) -> dict:
    """Cancel open orders for a symbol.

    If no filters are provided, this preserves the old behavior and asks Binance
    to cancel every open order on the symbol. In hedge mode, callers should
    prefer passing position_side to avoid cross-cancelling the opposite leg.
    """
    try:
        if position_side or protective_only:
            open_orders = await get_open_orders(client, symbol)
            if position_side is None:
                present_sides = {
                    str(order.get("positionSide", "")).upper()
                    for order in open_orders
                    if (not protective_only or is_protective_order(order))
                    and str(order.get("positionSide", "")).upper() in {"LONG", "SHORT"}
                }
                if len(present_sides) > 1:
                    print(
                        f"[Live] Refusing broad cancel_all_orders on {symbol}: "
                        f"multiple position sides active {sorted(present_sides)}"
                    )
                    return {
                        "symbol": symbol,
                        "cancelledOrders": [],
                        "skipped": True,
                        "reason": "multiple_position_sides_active",
                    }
            cancelled = []
            for order in open_orders:
                if protective_only and not is_protective_order(order):
                    continue
                if not order_matches_position_side(order, position_side):
                    continue
                res = await cancel_order(client, symbol, int(order["orderId"]))
                if res != {}:
                    cancelled.append(res)
            return {"symbol": symbol, "cancelledOrders": cancelled}
        return await _sreq(client, "DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
    except Exception as e:
        print(f"[Live] cancel_all_orders {symbol}: {e}")
        return {}


async def cancel_all_algo_orders(
    client: httpx.AsyncClient, symbol: str, position_side: str = None
) -> dict:
    """Cancel all open protective SL/TP algo orders for a symbol.

    Kept for backward compatibility with older orchestration code that still
    calls this helper under the old "algo" name.
    """
    try:
        open_orders = await get_algo_open_orders(client, symbol)
        if position_side is None:
            present_sides = {
                str(order.get("positionSide", "")).upper()
                for order in open_orders
                if str(order.get("positionSide", "")).upper() in {"LONG", "SHORT"}
            }
            if len(present_sides) > 1:
                print(
                    f"[Live] Refusing broad cancel_all_algo_orders on {symbol}: "
                    f"multiple position sides active {sorted(present_sides)}"
                )
                return {
                    "symbol": symbol,
                    "positionSide": None,
                    "cancelledOrders": [],
                    "skipped": True,
                    "reason": "multiple_position_sides_active",
                }
        cancelled = []
        for order in open_orders:
            if not order_matches_position_side(order, position_side):
                continue
            res = await cancel_algo_order(client, algo_id=int(order["algoId"]))
            if res != {}:
                cancelled.append(res)
        return {
            "symbol": symbol,
            "positionSide": position_side,
            "cancelledOrders": cancelled,
        }
    except Exception as e:
        print(f"[Live] cancel_all_algo_orders {symbol}: {e}")
        return {}


# ── Position / order queries ──────────────────────────────────────────────────

async def get_position_risk(client: httpx.AsyncClient, symbol: str = None) -> list:
    params = {}
    if symbol:
        params["symbol"] = symbol
    return await _sreq(client, "GET", "/fapi/v2/positionRisk", params)


async def get_open_orders(client: httpx.AsyncClient, symbol: str = None) -> list:
    params = {}
    if symbol:
        params["symbol"] = symbol
    return await _sreq(client, "GET", "/fapi/v1/openOrders", params)


async def get_algo_open_orders(
    client: httpx.AsyncClient, symbol: str = None, position_side: str = None
) -> list:
    """Return open protective SL/TP algo orders from the algo-order endpoint."""
    params = {}
    if symbol:
        params["symbol"] = symbol
    data = await _sreq(client, "GET", "/fapi/v1/openAlgoOrders", params)
    orders = data if isinstance(data, list) else data.get("orders", [])
    return [
        o for o in orders
        if is_protective_order(o) and order_matches_position_side(o, position_side)
    ]


async def get_order(
    client: httpx.AsyncClient,
    symbol: str,
    *,
    order_id: int | None = None,
    orig_client_order_id: str | None = None,
) -> dict:
    """Query order by orderId or origClientOrderId (for close PnL reconcile)."""
    params: dict = {"symbol": symbol}
    if order_id is not None:
        params["orderId"] = int(order_id)
    elif orig_client_order_id:
        params["origClientOrderId"] = orig_client_order_id
    else:
        raise ValueError("get_order requires order_id or orig_client_order_id")
    return await _sreq(client, "GET", "/fapi/v1/order", params)


async def get_account_trades(
    client: httpx.AsyncClient,
    symbol: str,
    start_time: int = None,
    end_time: int = None,
    limit: int = 1000,
) -> list:
    params = {"symbol": symbol, "limit": limit}
    if start_time:
        params["startTime"] = start_time
    if end_time:
        params["endTime"] = end_time
    return await _sreq(client, "GET", "/fapi/v1/userTrades", params)


async def get_account(client: httpx.AsyncClient) -> dict:
    """Fetch full account summary: wallet balance, available margin, positions."""
    return await _sreq(client, "GET", "/fapi/v2/account")


async def get_income(
    client: httpx.AsyncClient,
    income_type: str = None,
    start_time: int = None,
    end_time: int = None,
    limit: int = 1000,
    symbol: str = None,
) -> list:
    """Fetch account income history from /fapi/v1/income.

    incomeType values: TRANSFER, WELCOME_BONUS, REALIZED_PNL, FUNDING_FEE,
    COMMISSION, INSURANCE_CLEAR, REFERRAL_KICKBACK, COMMISSION_REBATE, etc.
    Omit income_type to fetch all record types.
    Each record: {"symbol","incomeType","income","asset","time","tranId"}.
    """
    params = {"limit": limit}
    if income_type:
        params["incomeType"] = income_type
    if start_time is not None:
        params["startTime"] = int(start_time)
    if end_time is not None:
        params["endTime"] = int(end_time)
    if symbol:
        params["symbol"] = symbol
    return await _sreq(client, "GET", "/fapi/v1/income", params)


# ── User Data Stream ──────────────────────────────────────────────────────────

def user_data_stream_ws_url(listen_key: str) -> str:
    """Binance futures UDS: listenKey in URL path (not query ?listenKey=)."""
    key = str(listen_key or "").strip()
    if not key:
        raise ValueError("listen_key required")
    return f"{WS_BASE}/private/ws/{key}"


async def create_listen_key(client: httpx.AsyncClient) -> str:
    data = await _sreq(client, "POST", "/fapi/v1/listenKey")
    return data["listenKey"]


async def fetch_open_algo_order_ids(client: httpx.AsyncClient, symbol: str = None) -> set:
    """Return the set of algoIds currently open on the exchange (authenticated).
    Used by fill detection to identify which position was closed when multiple
    same-side positions exist on the same symbol.
    """
    params = {}
    if symbol:
        params["symbol"] = symbol
    try:
        data = await _sreq(client, "GET", "/fapi/v1/openAlgoOrders", params)
        orders = data if isinstance(data, list) else data.get("orders", [])
        return {int(o["algoId"]) for o in orders if o.get("algoId")}
    except Exception:
        return set()


async def keepalive_listen_key(client: httpx.AsyncClient, listen_key: str):
    """Must be called every <30 min to keep the stream alive."""
    await sync_server_time(client)
    p = {
        "listenKey": listen_key,
        "timestamp": _timestamp_ms(),
        "recvWindow": RECV_WINDOW,
    }
    p["signature"] = _sign(p)
    await client.put(BASE_URL + "/fapi/v1/listenKey", params=p, headers=_headers())
