"""Hedge leg identity, client order IDs, and position registry."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

import binance_live
from config import TABS


def _core():
    from bot import core
    return core


def _algo_id(res: dict):
    c = _core()
    return int(res.get("algoId") or res.get("orderId"))


def _algo_trigger_price(order: dict):
    c = _core()
    return float(order.get("triggerPrice") or order.get("stopPrice") or 0)


def _algo_quantity(order: dict):
    c = _core()
    return float(order.get("quantity") or order.get("origQty") or 0)


def _algo_order_type(order: dict):
    c = _core()
    return str(order.get("orderType") or order.get("type") or "").upper()


def _is_active_algo_order(order: dict):
    c = _core()
    """Binance testnet can return cancelled/rejected rows from openAlgoOrders."""
    status = str(order.get("algoStatus") or order.get("status") or "NEW").upper()
    return status in {"NEW", "PARTIALLY_FILLED"}


def _active_algo_orders(data):
    c = _core()
    orders = data if isinstance(data, list) else data.get("orders", [])
    return [o for o in orders if c._is_active_algo_order(o)]


async def _verified_active_algo_orders(client, data):
    c = _core()
    """Filter Binance testnet stale rows by checking the order detail status."""
    active = []
    for order in c._active_algo_orders(data):
        sym = order.get("symbol")
        algo_id = order.get("algoId")
        client_algo_id = order.get("clientAlgoId")
        if not sym or not (algo_id or client_algo_id):
            active.append(order)
            continue
        detail = None
        for params in (
            {"symbol": sym, "algoId": algo_id} if algo_id else None,
            {"symbol": sym, "clientAlgoId": client_algo_id} if client_algo_id else None,
        ):
            if not params:
                continue
            try:
                detail = await binance_live._sreq(client, "GET", "/fapi/v1/algoOrder", params)
                break
            except Exception:
                continue
        if detail is None:
            # Testnet sometimes fails detail lookup for newly-created orders.
            # Keep the raw active row so protection is not accidentally ignored.
            active.append(order)
        elif c._is_active_algo_order(detail):
            merged = dict(order)
            merged.update(detail)
            active.append(merged)
    return active


def _position_side_name(side: str | None):
    c = _core()
    return "LONG" if side == "Long" else "SHORT"


def _position_side_from_state(pos: dict):
    c = _core()
    return str(pos.get("position_side") or c._position_side_name(pos.get("side"))).upper()


def _position_tuple_from_state(pos: dict):
    c = _core()
    return pos["symbol"], c._position_side_from_state(pos)


def _position_tuple_from_exchange(pos: dict):
    c = _core()
    side = str(pos.get("positionSide") or "").upper()
    if side not in {"LONG", "SHORT"}:
        side = "LONG" if float(pos.get("positionAmt", 0) or 0) > 0 else "SHORT"
    return pos["symbol"], side


STRATEGY_LABELS = {
    "Tab1": "EMA Pullback",
    "Tab2": "EMA Crossover",
    "Tab3": "SMC Order Block",
    "Tab4": "OTE",
    "Tab5": "RSI Divergence",
    "Tab6": "BB/KC Squeeze",
    "Tab7": "CCI 125",
    "Tab8": "Three Soldiers/Crows",
    "Tab9": "PA Impulse Continuation",
    "Tab10": "Vol Range",
    "SafeGuard": "SafeGuard Recovery",
    "Recovered": "Recovered Position",
}

# Telegram trade-notification emojis (restart server after changing)
TG_OPEN = "✔️"
TG_ENTRY = "📍"
TG_TP_CLOSE = "🚀🚀🚀"
TG_PROFIT_CLOSE = "💰"
TG_LOSS_CLOSE = "❌"


def _telegram_exit_icon(reason: str, net_pnl: float):
    c = _core()
    r = str(reason or "").upper()
    if r in ("TP", "TP_GAP") or r.startswith("TP"):
        return TG_TP_CLOSE
    return TG_PROFIT_CLOSE if net_pnl >= 0 else TG_LOSS_CLOSE


def _strategy_label(tab: str):
    c = _core()
    return STRATEGY_LABELS.get(tab, tab)


def _strategy_client_id(tab: str, position_side: str, role: str):
    c = _core()
    side = "L" if str(position_side).upper() == "LONG" else "S"
    stamp = datetime.now(timezone.utc).strftime("%y%m%d%H%M%S%f")[:15]
    return f"AG_{tab}_{side}_{role}_{stamp}"


def _strategy_from_client_id(value: str | None):
    c = _core()
    if not value:
        return None
    for part in str(value).split("_"):
        if part in c.TABS:
            return part
    return None


def _strategy_role_from_client_id(value: str | None):
    c = _core()
    if not value:
        return None
    parts = str(value).split("_")
    if len(parts) >= 4 and parts[0] == "AG" and parts[3] in {"ENTRY", "SL", "TP", "CLOSE", "CLOSEPM"}:
        return parts[3]
    return None


def _order_client_id(order: dict):
    c = _core()
    return (
        order.get("c")
        or order.get("C")
        or order.get("clientOrderId")
        or order.get("origClientOrderId")
    )


_BOT_CLOSE_FILL_TTL_SEC = 45
_recent_bot_close_fills: deque = deque(maxlen=100)


def _record_bot_close_fill(sym: str, position_side: str, close_side: str, qty: float, client_id: str | None = None):
    c = _core()
    _recent_bot_close_fills.append({
        "sym": str(sym).upper(),
        "position_side": str(position_side or "").upper(),
        "close_side": str(close_side or "").upper(),
        "qty": float(qty or 0),
        "client_id": client_id,
        "expires_at": datetime.now(timezone.utc).timestamp() + _BOT_CLOSE_FILL_TTL_SEC,
    })


def _prune_recent_bot_close_fills(now: float | None = None):
    c = _core()
    if now is None:
        now = datetime.now(timezone.utc).timestamp()
    while _recent_bot_close_fills and (
        _recent_bot_close_fills[0].get("expires_at", 0) <= now
        or _recent_bot_close_fills[0].get("consumed")
    ):
        _recent_bot_close_fills.popleft()


def _has_recent_bot_close_on_leg(sym: str, position_side: str, close_side: str):
    c = _core()
    """True when the bot recently closed any strategy on this hedge leg."""
    now = datetime.now(timezone.utc).timestamp()
    c._prune_recent_bot_close_fills(now)
    sym = str(sym).upper()
    position_side = str(position_side or "").upper()
    close_side = str(close_side or "").upper()
    for entry in _recent_bot_close_fills:
        if entry.get("sym") != sym:
            continue
        if position_side and entry.get("position_side") != position_side:
            continue
        if entry.get("close_side") != close_side:
            continue
        return True
    return False


def _consume_recent_bot_close_fill(sym: str, position_side: str, close_side: str, qty: float):
    c = _core()
    now = datetime.now(timezone.utc).timestamp()
    c._prune_recent_bot_close_fills(now)

    sym = str(sym).upper()
    position_side = str(position_side or "").upper()
    close_side = str(close_side or "").upper()
    qty = float(qty or 0)
    for entry in _recent_bot_close_fills:
        if entry.get("sym") != sym:
            continue
        if position_side and entry.get("position_side") != position_side:
            continue
        if entry.get("close_side") != close_side:
            continue
        qty_tol = max(abs(entry.get("qty", 0.0)) * 0.001, qty * 0.001, 1e-9)
        if abs(float(entry.get("qty", 0.0)) - qty) <= qty_tol:
            entry["consumed"] = True
            return True
    return False


def _strategy_from_algo_order(order: dict):
    c = _core()
    return c._strategy_from_client_id(
        order.get("clientAlgoId")
        or order.get("clientOrderId")
        or order.get("origClientOrderId")
    )


def _utc_now_iso():
    c = _core()
    return datetime.now(timezone.utc).isoformat()


def _position_entry_age_sec(pos: dict):
    c = _core()
    value = pos.get("entry_time")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return (datetime.now() - parsed).total_seconds()
        return (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()
    except Exception:
        return None


def _is_recent_entry(pos: dict, grace_sec: float | None = None):
    c = _core()
    if grace_sec is None:
        grace_sec = globals().get("c._SYNC_ENTRY_GRACE_SEC", 180.0)
    age = c._position_entry_age_sec(pos)
    return age is not None and 0 <= age < grace_sec


def _position_registry():
    c = _core()
    return c.state.setdefault("position_registry", {})


def _upsert_position_registry(pos_key: str, pos: dict, status: str = "open"):
    c = _core()
    reg = c._position_registry()
    now_iso = c._utc_now_iso()
    entry = reg.get(pos_key, {})
    entry.update({
        "pos_key": pos_key,
        "tab": pos.get("tab"),
        "symbol": pos.get("symbol"),
        "side": pos.get("side"),
        "position_side": c._position_side_from_state(pos),
        "entry_price": pos.get("entry_price"),
        "qty": pos.get("qty"),
        "entry_time": pos.get("entry_time"),
        "entry_order_id": pos.get("entry_order_id"),
        "entry_client_order_id": pos.get("entry_client_order_id"),
        "sl_client_algo_id": pos.get("sl_client_algo_id"),
        "tp_client_algo_id": pos.get("tp_client_algo_id"),
        "protection_mode": pos.get("protection_mode"),
        "status": status,
        "updated_at": now_iso,
    })
    for key in ("signal_sl", "signal_tp", "signal_entry_price", "placed_sl", "placed_tp"):
        if pos.get(key) is not None:
            entry[key] = pos.get(key)
    if pos.get("sl") is not None:
        entry["placed_sl"] = pos.get("placed_sl", pos.get("sl"))
    if pos.get("tp") is not None:
        entry["placed_tp"] = pos.get("placed_tp", pos.get("tp"))
    entry.setdefault("created_at", pos.get("entry_time") or now_iso)
    reg[pos_key] = entry
    c._prune_position_registry()


def _mark_position_registry_closed(pos_key: str, reason: str):
    c = _core()
    reg = c._position_registry()
    entry = reg.get(pos_key)
    if not entry:
        return
    entry["status"] = "closed"
    entry["closed_reason"] = reason
    entry["closed_at"] = c._utc_now_iso()
    entry["updated_at"] = entry["closed_at"]


def _prune_position_registry(limit: int = 1000):
    c = _core()
    reg = c._position_registry()
    if len(reg) <= limit:
        return
    closed = [
        (k, v.get("closed_at") or v.get("updated_at") or "")
        for k, v in reg.items()
        if v.get("status") == "closed"
    ]
    closed.sort(key=lambda item: item[1])
    for key, _ in closed[:max(0, len(reg) - limit)]:
        reg.pop(key, None)


def _strategy_from_position_registry(
    sym: str,
    pos_side: str,
    side: str,
    entry_px: float,
    qty: float,
):
    c = _core()
    candidates = []
    for pos_key, entry in c._position_registry().items():
        if entry.get("status") == "closed":
            continue
        tab = entry.get("tab")
        if not tab or tab == "SafeGuard":
            continue
        if entry.get("symbol") != sym:
            continue
        if str(entry.get("position_side") or "").upper() != str(pos_side).upper():
            continue
        if entry.get("side") != side:
            continue
        try:
            reg_qty = float(entry.get("qty") or 0)
            reg_entry = float(entry.get("entry_price") or 0)
        except Exception:
            continue
        if reg_qty <= 0:
            continue
        qty_score = abs(reg_qty - qty) / max(reg_qty, qty, 1e-12)
        if qty_score > 0.35:
            continue
        px_score = abs(reg_entry - entry_px) / max(reg_entry, entry_px, 1e-12) if reg_entry > 0 and entry_px > 0 else 0.0
        if px_score > 0.03:
            continue
        candidates.append((qty_score + px_score, pos_key, tab))

    if not candidates:
        return None, None
    candidates.sort(key=lambda item: item[0])
    _, pos_key, tab = candidates[0]
    return tab, f"position registry {pos_key}"
