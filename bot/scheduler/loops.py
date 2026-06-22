"""Background scheduler and income polling loops (extracted from bot.core)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import binance_live
import pionex_live
from config import (
    ENTRY_BUSY_BUFFER_SEC,
    ENTRY_EVAL_BUDGET_SEC,
    KLINE_FETCH_DELAY_SEC,
    PIONEX_BALANCE_POLL_SEC,
)

def _core():
    from bot import core
    return core


async def _run_startup_invalidations():
    c = _core()
    """Startup invalidation pass without blocking scheduler heartbeats."""
    await asyncio.sleep(15)
    if c._eval_lock.locked():
        print("[Scheduler] Startup invalidations skipped — eval lock busy")
        return
    try:
        async with c._eval_lock:
            await c.check_invalidations_loop(force=True)
    except Exception as e:
        await c.record_error_event(
            f"Startup invalidations error: {e}",
            severity="warning",
            source="scheduler",
            notify=False,
        )
        print(f"[Scheduler] Startup invalidations error: {e}")


async def scheduler_loop():
    c = _core()
    """Auto signal evaluation every 1H candle close + midnight circuit-breaker reset."""
    # On startup: only check invalidations, do NOT open new orders (wait for fresh candle)
    c._mark_heartbeat("scheduler")
    asyncio.create_task(_run_startup_invalidations())

    last_scan_slot: datetime | None = None
    last_midnight_reset_date: str | None = None
    while True:
        c._mark_heartbeat("scheduler")
        now = datetime.now(timezone.utc)
        next_wake = now
        try:
            hour_slot = now.replace(minute=0, second=0, microsecond=0)
            scan_at = c._candle_scan_at(hour_slot)

            await c.maybe_run_kline_prescreen(now)

            # Midnight circuit-breaker reset (after scan+entry on hour 0)
            if (
                hour_slot.hour == 0
                and now >= scan_at
                and c.LIVE_MODE
                and last_midnight_reset_date != now.strftime("%Y-%m-%d")
            ):
                was_active = c._circuit_breaker
                c._circuit_breaker = False
                c._daily_loss_usd  = 0.0
                c._daily_loss_date = now.strftime("%Y-%m-%d")
                last_midnight_reset_date = c._daily_loss_date
                c._persist_circuit_breaker()
                await c.save_state()
                print(f"[Scheduler] Midnight reset — circuit breaker {'released' if was_active else 'OK'}, daily loss cleared")

            # Kline scan at close+c.KLINE_FETCH_DELAY_SEC, then staggered entries (no batch wait).
            if now >= scan_at and last_scan_slot != hour_slot:
                last_scan_slot = hour_slot
                if not c._eval_lock.locked():
                    async with c._eval_lock:
                        started = datetime.now(timezone.utc)
                        print(
                            f"[Scheduler] Kline scan for candle {hour_slot:%Y-%m-%d %H:%M:%S} UTC "
                            f"at {started:%H:%M:%S} UTC (+{c._effective_kline_fetch_delay_sec()}s)"
                        )
                        c._begin_entry_window()
                        c._reset_entry_stagger()
                        try:
                            await c.check_invalidations_loop(hour_slot=hour_slot)
                            scan_4h = None
                            if hour_slot.hour % 4 == 0 and c._interval_has_enabled_tabs("4h"):
                                scan_4h = await c.scan_candle_signals("4h", hour_slot=hour_slot)
                            scan_1h = None
                            if c._interval_has_enabled_tabs("1h"):
                                scan_1h = await c.scan_candle_signals("1h", hour_slot=hour_slot)
                            if scan_4h:
                                await c.execute_scanned_entries(scan_4h)
                            if scan_1h:
                                await c.execute_scanned_entries(scan_1h)
                        finally:
                            c._release_entry_busy_after_eval()
                        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                        print(f"[Scheduler] Scan+entry finished in {elapsed:.2f}s")
                        c._mark_heartbeat("scheduler")

            next_wake = c._next_scheduler_wake_at(now, last_scan_slot)
        except Exception as e:
            await c.record_error_event(
                f"scheduler_loop error: {e}",
                severity="warning",
                source="scheduler",
                notify=False,
            )
            print(f"[Scheduler] error: {e}")
            next_wake = datetime.now(timezone.utc)
        await asyncio.sleep(max(1, min(30, (next_wake - datetime.now(timezone.utc)).total_seconds())))

async def exchange_account_loop():
    c = _core()
    """Live account snapshot: UDS ACCOUNT_UPDATE when fresh; REST poll + sync on a slower cadence."""
    _last_sync_time = 0.0
    _last_margin_sample = 0.0
    import time as _time_mod
    while True:
        uds_fresh = c._uds_account_fresh()
        poll_sec = c.EXCHANGE_ACCOUNT_POLL_SEC_UDS if uds_fresh else c.EXCHANGE_ACCOUNT_POLL_SEC_NO_UDS
        sync_sec = c.EXCHANGE_ACCOUNT_SYNC_SEC_UDS if uds_fresh else c.EXCHANGE_ACCOUNT_SYNC_SEC_NO_UDS
        if uds_fresh and c._uds_connected:
            sync_sec = max(sync_sec, c.EXCHANGE_ACCOUNT_SYNC_SEC_UDS_CONNECTED)
        try:
            now_ts = _time_mod.monotonic()
            if not c._binance_rate_limited() and (not uds_fresh or not c.exchange_account):
                await c._fetch_exchange_account_rest()
                async with c._state_lock:
                    c._recalculate_unrealized_pnls()
            elif uds_fresh:
                async with c._state_lock:
                    c._recalculate_unrealized_pnls()

            if c.exchange_account and now_ts - _last_margin_sample >= c._MARGIN_SAMPLE_INTERVAL_SEC:
                _last_margin_sample = now_ts
                mb = float(c.exchange_account.get("marginBalance", 0) or 0)
                if mb > 0:
                    mh = c.state.setdefault("margin_history", [])
                    mh.append({
                        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                        "value": mb,
                    })
                    if len(mh) > c._MARGIN_HISTORY_CAP:
                        del mh[:len(mh) - c._MARGIN_HISTORY_CAP]
                    await c.save_state()

            if (
                not c._binance_rate_limited()
                and now_ts - _last_sync_time >= sync_sec
            ):
                _last_sync_time = now_ts
                await c.sync_live_positions()

        except Exception as e:
            c._last_exchange_account_error_at = c._utc_now_iso()
            c._note_binance_rate_limit(e)
            await c.record_error_event(
                f"exchange_account_loop error: {e}",
                severity="critical",
                source="c.exchange_account",
                notify=True,
            )
            print(f"[Live] exchange_account_loop error: {e}")
        await asyncio.sleep(poll_sec)


async def pionex_balance_loop():
    c = _core()
    """Poll Pionex wallet/balancesFull for dashboard card (independent of c.LIVE_MODE)."""
    if not c.PIONEX_CONFIGURED:
        return
    c._pionex_balance_snapshot = {
        **c._pionex_balance_snapshot,
        "configured": True,
    }
    while True:
        try:
            data = await pionex_live.fetch_balances_full(c._http_client)
            c._pionex_balance_snapshot = {
                "configured": True,
                "ok": True,
                "error": None,
                "updated_at": c._utc_now_iso(),
                **data,
            }
        except Exception as e:
            c._pionex_balance_snapshot = {
                "configured": True,
                "ok": False,
                "total_in_usdt": c._pionex_balance_snapshot.get("total_in_usdt"),
                "total_in_btc": c._pionex_balance_snapshot.get("total_in_btc"),
                "bot_account_usdt": c._pionex_balance_snapshot.get("bot_account_usdt"),
                "trader_account_usdt": c._pionex_balance_snapshot.get("trader_account_usdt"),
                "total_in_thb": c._pionex_balance_snapshot.get("total_in_thb"),
                "usdt_thb_rate": c._pionex_balance_snapshot.get("usdt_thb_rate"),
                "error": str(e),
                "updated_at": c._utc_now_iso(),
            }
            print(f"[Pionex] balance poll error: {e}")
        await asyncio.sleep(c.PIONEX_BALANCE_POLL_SEC)


async def process_watchdog_loop():
    c = _core()
    """Lightweight in-process watchdog for core background loop heartbeats."""
    import time as _time_mod
    account_stale_sec, sync_stale_sec = c._health_stale_thresholds()
    checks: dict[str, tuple] = {
        "price websocket stale": (lambda: c._iso_age_seconds(c._last_price_ws_ok_at), 120.0),
        "scheduler heartbeat stale": (lambda: c._iso_age_seconds(c._last_scheduler_ok_at), 120.0),
    }
    if c.LIVE_MODE:
        checks["position sync stale"] = (lambda: c._iso_age_seconds(c._last_sync_ok_at), sync_stale_sec)
        checks["account snapshot stale"] = (
            lambda: c._iso_age_seconds(c._last_exchange_account_ok_at),
            account_stale_sec,
        )

    while True:
        c._mark_heartbeat("watchdog")
        account_stale_sec, sync_stale_sec = c._health_stale_thresholds()
        if c.LIVE_MODE:
            checks["position sync stale"] = (lambda: c._iso_age_seconds(c._last_sync_ok_at), sync_stale_sec)
            checks["account snapshot stale"] = (
                lambda: c._iso_age_seconds(c._last_exchange_account_ok_at),
                account_stale_sec,
            )
        for label, (age_fn, max_age) in checks.items():
            age = age_fn()
            if age is not None and age <= max_age:
                continue
            now = _time_mod.monotonic()
            last_alert = c._watchdog_last_alert_at.get(label, 0.0)
            if now - last_alert < 600:
                continue
            c._watchdog_last_alert_at[label] = now
            age_text = "never" if age is None else f"{age}s"
            await c.record_error_event(
                f"Process watchdog: {label} (age={age_text})",
                severity="warning",
                source="process_watchdog",
                notify=False,
            )
        await asyncio.sleep(30)


async def _rebuild_binance_gross_breakdown(force: bool = False):
    c = _core()
    """Backfill account/tab gross profit/loss from /fapi/v1/income (realized + fee + funding)."""
    if not c.LIVE_MODE or c._http_client is None or c._binance_rate_limited():
        return
    inc = c._normalize_binance_income_state()
    if inc.get("gross_rebuilt") and not force:
        return
    tabs = c._normalize_binance_tab_income()
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    cursor = int(inc.get("gross_seed_ts", 0) or 0)

    inc["gross_profit"] = 0.0
    inc["gross_loss"] = 0.0
    gross_seen: set[int] = set()
    for tab in list(tabs.keys()):
        tabs[tab] = c._empty_tab_income()

    pulled_total = 0
    while True:
        params_start = cursor if cursor > 0 else 1
        records = await binance_live.get_income(
            c._http_client,
            start_time=params_start,
            end_time=end_ms,
            limit=1000,
        )
        if not records:
            break
        for r in records:
            c._apply_income_gross_record(r, inc=inc, tabs=tabs, seen=gross_seen)
        pulled_total += len(records)
        if len(records) < 1000:
            break
        cursor = int(records[-1].get("time") or cursor) + 1
        if cursor >= end_ms:
            break

    inc["gross_rebuilt"] = True
    if gross_seen:
        inc["income_tran_high_water"] = max(
            int(inc.get("income_tran_high_water", 0) or 0),
            max(gross_seen),
        )
    net = c._binance_gross_net(inc["gross_profit"], inc["gross_loss"])
    c._append_equity_snapshot(force=True)
    await c.save_state()
    print(
        f"[Income Sync] gross breakdown rebuilt ({pulled_total} income rows) | "
        f"profit=${inc['gross_profit']:.2f} loss=${inc['gross_loss']:.2f} "
        f"net=${net:.2f} (incl. funding)"
    )


async def _sync_income_once(*, refresh_today: bool = False):
    c = _core()
    """One-shot pull of /fapi/v1/income since last cursor. Safe to call concurrently
    with the background loop — c.state writes are coalesced by c.save_state()."""
    if not c.LIVE_MODE or c._binance_rate_limited():
        return
    import time as _time_mod
    inc = c._normalize_binance_income_state()
    tabs = c._normalize_binance_tab_income()
    seen = c._income_seen_tran_set(inc)
    cursor = int(inc.get("last_ts", 0) or 0)
    start = cursor + 1 if cursor > 0 else None
    pulled_total = 0
    applied_total = 0
    while True:
        records = await binance_live.get_income(
            c._http_client, start_time=start, limit=1000,
        )
        if not records:
            break
        for r in records:
            if c._apply_income_record(r, inc=inc, tabs=tabs, seen=seen):
                applied_total += 1
        pulled_total += len(records)
        if len(records) < 1000:
            break
        start = inc["last_ts"] + 1
    if applied_total > 0:
        c._persist_income_seen_tran(inc, seen)
        c._append_equity_snapshot()
        await c.save_state()
        net = inc["realized_pnl"] + inc["commission"] + inc["funding"]
        fetched_note = f" ({pulled_total} fetched)" if pulled_total > applied_total else ""
        print(f"[Income Sync] +{applied_total} new income rows{fetched_note} | "
              f"realized=${inc['realized_pnl']:.2f} "
              f"commission=${inc['commission']:.2f} "
              f"funding=${inc['funding']:.2f} "
              f"profit=${inc['gross_profit']:.2f} loss=${inc['gross_loss']:.2f} "
              f"net=${net:.2f}")
    now_mono = _time_mod.monotonic()
    if refresh_today or (
        now_mono - c._last_income_today_sync_mono >= c._INCOME_TODAY_REFRESH_SEC
    ):
        try:
            await c._sync_today_income_once()
        except Exception as e:
            print(f"[Income Sync] today profit refresh error: {e}")
    await c._maybe_sync_daily_profit_30d()


async def trigger_income_sync(delay_sec: float = 8.0):
    c = _core()
    """Fire-and-forget: wait briefly then pull latest income. Use after close_position."""
    try:
        await asyncio.sleep(delay_sec)
        await _sync_income_once(refresh_today=True)
    except Exception as e:
        print(f"[Income Sync] triggered pull error: {e}")


async def server_time_sync_loop():
    c = _core()
    """Keep signed Binance request timestamps aligned with exchange server time."""
    if not c.LIVE_MODE:
        return
    await asyncio.sleep(binance_live.TIME_SYNC_INTERVAL_SEC)
    while True:
        if c._binance_rate_limited():
            await asyncio.sleep(30)
            continue
        try:
            await binance_live.sync_server_time(c._http_client, force=True)
        except Exception as e:
            print(f"[Live] server_time_sync_loop: {e}")
            c._note_binance_rate_limit(e)
        await asyncio.sleep(binance_live.TIME_SYNC_INTERVAL_SEC)


async def income_sync_loop():
    c = _core()
    """Poll /fapi/v1/income periodically so `binance_income` tracks Binance truth."""
    if not c.LIVE_MODE:
        return
    import time as _time_mod
    await asyncio.sleep(20)
    while True:
        try:
            if not c._binance_rate_limited():
                await _sync_income_once()
        except Exception as e:
            print(f"[Income Sync] error: {e}")
            c._note_binance_rate_limit(e)
        since_close = _time_mod.monotonic() - c._last_live_close_mono
        poll_sec = (
            c._INCOME_SYNC_POLL_ACTIVE_SEC
            if since_close < c._INCOME_SYNC_IDLE_AFTER_CLOSE_SEC
            else c._INCOME_SYNC_POLL_SEC
        )
        await asyncio.sleep(poll_sec)
