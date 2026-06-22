"""Entry/position SL-TP protection policy (extracted from bot.core)."""

from __future__ import annotations


def _core():
    from bot import core
    return core


def _position_sl_is_local(pos: dict):
    c = _core()
    src = str(pos.get("sl_source") or "").lower()
    if src == "local":
        return True
    if src == "exchange":
        return False
    return str(pos.get("protection_mode") or "").lower() == "local"


def _position_tp_is_local(pos: dict):
    c = _core()
    src = str(pos.get("tp_source") or "").lower()
    if src == "local":
        return True
    if src == "exchange":
        return False
    return str(pos.get("protection_mode") or "").lower() == "local"


def _vanished_exchange_protection_reason(pos: dict, open_algo_ids: set[int]):
    c = _core()
    """Infer SL/TP when an exchange algo triggers as a MARKET fill on the user stream."""
    sl_oid = pos.get("sl_order_id")
    tp_oid = pos.get("tp_order_id")
    sl_gone = (
        sl_oid
        and not c._position_sl_is_local(pos)
        and int(sl_oid) not in open_algo_ids
    )
    tp_gone = (
        tp_oid
        and not c._position_tp_is_local(pos)
        and int(tp_oid) not in open_algo_ids
    )
    if sl_gone and not tp_gone:
        return "SL"
    if tp_gone and not sl_gone:
        return "TP"
    if sl_gone and tp_gone:
        return "SL"
    return None


def _position_full_local(pos: dict):
    c = _core()
    return c._position_sl_is_local(pos) and c._position_tp_is_local(pos)


def _position_needs_local_exit_monitor(pos: dict):
    c = _core()
    return c.LIVE_MODE and (c._position_sl_is_local(pos) or c._position_tp_is_local(pos))


def _resolve_entry_protection_plan(
    mode: str,
    algo_count: int | None,
):
    c = _core()
    """Return (sl_local, tp_local, reason) or None when entry must be skipped."""
    free = None if algo_count is None else (c._max_open_algo_orders() - algo_count)
    if mode == "local":
        return True, True, c._MAINNET_LOCAL_SLTP_REASON
    if mode == "binance":
        if free is not None and free < 2:
            return None
        return False, False, c._BINANCE_EXCHANGE_REASON
    if mode == "hybrid":
        if free is not None and free < 1:
            return None
        return False, True, c._HYBRID_SLTP_REASON
    if mode == "binance_fallback":
        if free is not None and free < 2:
            return True, True, c._FALLBACK_LOCAL_REASON
        return False, False, c._BINANCE_EXCHANGE_REASON
    return True, True, c._MAINNET_LOCAL_SLTP_REASON


def _apply_protection_sources(
    pos: dict,
    *,
    sl_local: bool,
    tp_local: bool,
    reason: str,
):
    c = _core()
    pos["sl_source"] = "local" if sl_local else "exchange"
    pos["tp_source"] = "local" if tp_local else "exchange"
    if sl_local and tp_local:
        pos["protection_mode"] = "local"
        pos["protection_reason"] = reason
        pos["protection_status"] = "local"
        pos["sl_order_id"] = None
        pos["tp_order_id"] = None
        pos["sl_client_algo_id"] = None
        pos["tp_client_algo_id"] = None
    elif not sl_local and not tp_local:
        pos.pop("protection_mode", None)
        pos["protection_reason"] = reason
        pos["protection_status"] = "exchange"
    else:
        pos["protection_mode"] = "hybrid"
        pos["protection_reason"] = reason
        pos["protection_status"] = "hybrid"
        if tp_local:
            pos["tp_order_id"] = None
            pos["tp_client_algo_id"] = None
        if sl_local:
            pos["sl_order_id"] = None
            pos["sl_client_algo_id"] = None


def _policy_cancels_untracked_exchange_algos():
    c = _core()
    return c._effective_sltp_mode() == "local"
