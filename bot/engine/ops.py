"""Telegram, error events, and health snapshot (extracted from bot.core)."""

from __future__ import annotations

from datetime import datetime, timezone


def _core():
    from bot import core
    return core


_ERROR_EVENTS_CAP = 200
_ERROR_EVENT_DEDUP_SEC = 120.0
_error_event_recent: dict[str, float] = {}


def _is_tp_sl_exit_reason(reason: str | None):
    c = _core()
    r = str(reason or "").upper()
    if r == "SL":
        return True
    return r == "TP" or r.startswith("TP")


def _telegram_allowed(*, exit_reason: str | None = None, is_error: bool = False):
    c = _core()
    """Telegram sends only errors (not TP/SL exit fills)."""
    return is_error


async def send_telegram(message: str, *, exit_reason: str | None = None, is_error: bool = False):
    c = _core()
    """Send a message via Telegram Bot API. Silently skips if disabled or not configured.

    Policy: only errors (is_error=True). TP/SL exit fills are logged, not Telegrammed.

    Uses a dedicated short-timeout httpx client so a slow Telegram endpoint can
    never block Binance-facing requests.
    """
    if not c.TELEGRAM_ENABLED or not c.TELEGRAM_BOT_TOKEN or not c.TELEGRAM_CHAT_ID:
        return
    if not c._telegram_allowed(exit_reason=exit_reason, is_error=is_error):
        return
    client = c._telegram_client or c._http_client
    if client is None:
        return
    try:
        url = f"https://api.telegram.org/bot{c.TELEGRAM_BOT_TOKEN}/sendMessage"
        await client.post(url, json={
            "chat_id": c.TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        })
    except Exception as e:
        print(f"[Telegram] Send error: {e}")


def _iso_age_seconds(value: str | None):
    c = _core()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    except Exception:
        return None


def _mark_heartbeat(name: str):
    c = _core()
    now = c._utc_now_iso()
    if name == "price_ws":
        c._last_price_ws_ok_at = now
    elif name == "scheduler":
        c._last_scheduler_ok_at = now
    elif name == "watchdog":
        c._last_watchdog_ok_at = now


async def record_error_event(
    message: str,
    severity: str = "error",
    source: str = "runtime",
    notify: bool = False,
):
    c = _core()
    """Persist a small deduped operator-facing error event for the dashboard."""
    import time as _time_mod

    now = _time_mod.time()
    for key in [k for k, ts in _error_event_recent.items() if now - ts > _ERROR_EVENT_DEDUP_SEC]:
        _error_event_recent.pop(key, None)

    clean_message = str(message).strip()
    dedup_key = f"{source}|{severity}|{clean_message[:160]}"
    if dedup_key in _error_event_recent:
        return
    _error_event_recent[dedup_key] = now

    events = c.state.setdefault("error_events", [])
    events.append({
        "message": clean_message,
        "severity": severity,
        "source": source,
        "created_at": c._utc_now_iso(),
    })
    if len(events) > _ERROR_EVENTS_CAP:
        del events[:len(events) - _ERROR_EVENTS_CAP]

    try:
        await c.save_state()
    except Exception:
        pass

    if notify:
        snippet = clean_message if len(clean_message) <= 1200 else clean_message[:1200] + "..."
        try:
            await c.send_telegram(
                f"🚨 <b>{severity.upper()}</b> [{source}]\n<pre>{snippet}</pre>",
                is_error=True,
            )
        except Exception:
            pass


def _expected_local_protection(pos: dict):
    c = _core()
    """True when bot-managed leg(s) are intentional policy, not a fallback failure."""
    reason = str(pos.get("protection_reason") or "")
    mode = c._effective_sltp_mode()
    if c._position_full_local(pos):
        if mode in ("local", "binance_fallback") or reason in c._LOCAL_SLTP_POLICY_REASONS:
            return True
    if str(pos.get("protection_mode") or "").lower() == "hybrid":
        if c._position_tp_is_local(pos) and not c._position_sl_is_local(pos) and mode == "hybrid":
            return bool(pos.get("sl_order_id"))
    if mode == "binance" and c._position_full_local(pos):
        return False
    if c._effective_local_sltp():
        return c._position_full_local(pos)
    return reason in c._LOCAL_SLTP_POLICY_REASONS


def _position_protection_risk(pos_key: str, pos: dict):
    c = _core()
    status = str(pos.get("protection_status") or "").lower()
    mode = str(pos.get("protection_mode") or "").lower()
    has_sl = bool(pos.get("sl_order_id"))
    has_tp = bool(pos.get("tp_order_id"))
    tab = pos.get("tab") or "Unknown"
    sym = pos.get("symbol") or pos_key
    side = pos.get("side") or c._position_side_from_state(pos)

    def _risk(level: str, label: str, detail: str) -> dict:
        return {
            "key": pos_key,
            "level": level,
            "status": label,
            "message": f"{pos_key}: {detail}",
            "tab": tab,
            "symbol": sym,
            "side": side,
            "recovery_source": pos.get("recovery_source"),
        }

    if status == "protection_failed" or mode == "failed":
        reason = pos.get("protection_reason") or "SL/TP placement failed"
        return _risk("critical", "protection_failed", f"protection failed ({reason})")
    if status == "verify_warning":
        msg = pos.get("entry_verify_message") or "entry protection verification warning"
        return _risk("critical", "verify_warning", msg)
    if status == "entry_saved_pending_protection":
        return _risk("warning", "pending_protection", "entry saved while protection is still pending")
    if mode == "local":
        if c._expected_local_protection(pos):
            return None
        return _risk("warning", "local", "using bot-managed local SL/TP instead of Binance server-side SL/TP")
    missing: list[str] = []
    if str(pos.get("protection_mode") or "").lower() == "hybrid" or (
        c._position_tp_is_local(pos) != c._position_sl_is_local(pos)
    ):
        if not c._position_sl_is_local(pos) and not has_sl:
            return _risk("critical", "missing_protection", "missing tracked SL protection (hybrid)")
        if c._expected_local_protection(pos):
            return None
    if not has_sl and not c._position_sl_is_local(pos):
        if c._expected_local_protection(pos):
            return None
        missing.append("SL")
    if not has_tp and not c._position_tp_is_local(pos):
        if c._expected_local_protection(pos):
            return None
        missing.append("TP")
    if missing:
        return _risk("critical", "missing_protection", f"missing tracked {'/'.join(missing)} protection")
    return None


def _position_protection_risks():
    c = _core()
    risks = []
    for pos_key, pos in c.state.get("open_positions", {}).items():
        risk = c._position_protection_risk(pos_key, pos)
        if risk:
            risks.append(risk)
    return risks


def _health_snapshot():
    c = _core()
    events = c.state.get("error_events", [])
    now = datetime.now(timezone.utc)
    error_count_1h = 0
    error_count_24h = 0
    critical_1h = 0
    for ev in events:
        try:
            created = datetime.fromisoformat(str(ev.get("created_at", "")).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        age = (now - created).total_seconds()
        if age <= 3600:
            error_count_1h += 1
            if ev.get("severity") == "critical":
                critical_1h += 1
        if age <= 86400:
            error_count_24h += 1

    sync_age = c._iso_age_seconds(c._last_sync_ok_at)
    account_age = c._iso_age_seconds(c._last_exchange_account_ok_at)
    price_ws_age = c._iso_age_seconds(c._last_price_ws_ok_at)
    scheduler_age = c._iso_age_seconds(c._last_scheduler_ok_at)
    watchdog_age = c._iso_age_seconds(c._last_watchdog_ok_at)
    sync_issue_count = len(c.state.get("sync_issues", []))
    protection_risks = c._position_protection_risks() if c.LIVE_MODE else []
    critical_protection = [r for r in protection_risks if r.get("level") == "critical"]
    warning_protection = [r for r in protection_risks if r.get("level") == "warning"]

    status = "ok"
    reasons = []
    if c.LIVE_MODE:
        account_stale_sec, sync_stale_sec = c._health_stale_thresholds()
        if not c._uds_connected:
            status = "warning"
            reasons.append("UDS disconnected")
        if account_age is None or account_age > account_stale_sec:
            status = "warning"
            reasons.append("account snapshot stale")
        if sync_age is None or sync_age > sync_stale_sec:
            status = "warning"
            reasons.append("position sync stale")
    if price_ws_age is None or price_ws_age > 120:
        status = "warning"
        reasons.append("price websocket stale")
    if scheduler_age is None or scheduler_age > 120:
        status = "warning"
        reasons.append("scheduler heartbeat stale")
    if watchdog_age is None or watchdog_age > 120:
        status = "warning"
        reasons.append("watchdog heartbeat stale")
    if sync_issue_count:
        status = "warning"
        reasons.append(f"{sync_issue_count} sync issue(s)")
    if warning_protection and status == "ok":
        status = "warning"
        reasons.append(f"{len(warning_protection)} local/pending protection warning(s)")
    if critical_protection:
        status = "critical"
        reasons.append(f"{len(critical_protection)} unprotected position(s)")
    if critical_1h:
        status = "critical"
        reasons.append(f"{critical_1h} critical event(s) in 1h")
    ban = c._binance_rate_limit_snapshot()
    if ban.get("active"):
        status = "warning" if status == "ok" else status
        remaining = int(ban.get("remaining_sec") or 0)
        reasons.insert(0, f"Binance API ban ({remaining}s left)")

    return {
        "status": status,
        "reasons": reasons,
        "binance_rate_limit": ban,
        "uds_connected": c._uds_connected if c.LIVE_MODE else None,
        "last_uds_connected_at": c._last_uds_connected_at,
        "last_uds_error_at": c._last_uds_error_at,
        "last_sync_ok_at": c._last_sync_ok_at,
        "last_sync_error_at": c._last_sync_error_at,
        "last_price_ws_ok_at": c._last_price_ws_ok_at,
        "last_scheduler_ok_at": c._last_scheduler_ok_at,
        "last_watchdog_ok_at": c._last_watchdog_ok_at,
        "last_exchange_account_ok_at": c._last_exchange_account_ok_at,
        "last_exchange_account_error_at": c._last_exchange_account_error_at,
        "last_sync_age_sec": sync_age,
        "last_price_ws_age_sec": price_ws_age,
        "last_scheduler_age_sec": scheduler_age,
        "last_watchdog_age_sec": watchdog_age,
        "last_exchange_account_age_sec": account_age,
        "open_positions": len(c.state.get("open_positions", {})),
        "protection_risks": protection_risks[-10:],
        "protection_risk_count": len(protection_risks),
        "critical_protection_count": len(critical_protection),
        "warning_protection_count": len(warning_protection),
        "sync_issue_count": sync_issue_count,
        "error_count_1h": error_count_1h,
        "error_count_24h": error_count_24h,
        "recent_errors": events[-5:],
    }
