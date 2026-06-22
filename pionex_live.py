"""
pionex_live.py — Pionex Wallet API helpers (read-only balance for dashboard).

Base URL: https://api.pionex.com
Endpoint: GET /api/v1/wallet/balancesFull
"""

import hashlib
import hmac
import time

import httpx

from config import PIONEX_API_KEY, PIONEX_API_SECRET, PIONEX_USDT_THB_RATE

BASE_URL = "https://api.pionex.com"
BALANCES_FULL_PATH = "/api/v1/wallet/balancesFull"
BINANCE_TH_URL = "https://api.binance.th"
FRANKFURTER_URL = "https://api.frankfurter.app"


async def fetch_usdt_thb_rate(client: httpx.AsyncClient) -> float | None:
    """USDT/THB for dashboard — Binance TH spot, then USD/THB fallback."""
    if PIONEX_USDT_THB_RATE and PIONEX_USDT_THB_RATE > 0:
        return float(PIONEX_USDT_THB_RATE)

    sources = (
        (f"{BINANCE_TH_URL}/api/v1/ticker/price", {"symbol": "USDTTHB"}, "price"),
        (f"{FRANKFURTER_URL}/latest", {"from": "USD", "to": "THB"}, "rates"),
    )
    for url, params, kind in sources:
        try:
            r = await client.get(url, params=params, timeout=10.0)
            r.raise_for_status()
            body = r.json()
            if kind == "price":
                price = float(body.get("price", 0) or 0)
            else:
                price = float((body.get("rates") or {}).get("THB", 0) or 0)
            if price > 0:
                return price
        except Exception:
            continue
    return None


def _timestamp_ms() -> int:
    return int(time.time() * 1000)


def _build_signed_get(path: str, params: dict | None = None) -> tuple[str, dict, dict]:
    """Return (url, query_params, headers) for a signed GET request."""
    p = dict(params or {})
    p["timestamp"] = _timestamp_ms()
    query = "&".join(f"{k}={p[k]}" for k in sorted(p))
    path_url = f"{path}?{query}"
    message = f"GET{path_url}"
    signature = hmac.new(
        PIONEX_API_SECRET.encode(), message.encode(), hashlib.sha256
    ).hexdigest()
    headers = {
        "PIONEX-KEY": PIONEX_API_KEY,
        "PIONEX-SIGNATURE": signature,
    }
    return BASE_URL + path_url, p, headers


async def fetch_balances_full(client: httpx.AsyncClient) -> dict:
    """Fetch full wallet balance overview from Pionex."""
    url, _, headers = _build_signed_get(BALANCES_FULL_PATH)
    r = await client.get(url, headers=headers)
    r.raise_for_status()
    body = r.json()
    if not body.get("result"):
        code = body.get("code", "UNKNOWN")
        msg = body.get("message", "Pionex API error")
        raise RuntimeError(f"{code}: {msg}")

    data = body.get("data") or {}
    bot = data.get("botAccount") or {}
    trader = data.get("traderAccount") or {}
    total_usdt = _float_or_none(data.get("totalInUsdt"))
    usdt_thb_rate = await fetch_usdt_thb_rate(client)
    total_thb = (
        total_usdt * usdt_thb_rate
        if total_usdt is not None and usdt_thb_rate is not None
        else None
    )
    return {
        "total_in_usdt": total_usdt,
        "total_in_btc": _float_or_none(data.get("totalInBtc")),
        "bot_account_usdt": _float_or_none(bot.get("totalInUsdt")),
        "trader_account_usdt": _float_or_none(trader.get("totalInUsdt")),
        "total_in_thb": total_thb,
        "usdt_thb_rate": usdt_thb_rate,
    }


def _float_or_none(val) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
