"""Default paper_state.json schema (single source for fresh/corrupt recovery)."""

from __future__ import annotations

from datetime import datetime, timezone

from config import (
    INITIAL_BALANCE,
    LEVERAGE,
    MAX_POSITIONS_PER_TAB,
    NOTIONAL_SIZE,
    SLTP_MODE,
    SYMBOL_SCAN_LIMIT,
    TABS,
)
from bot.engine.premium_config import default_tab17_scan_limit_by_tab


def default_state(
    *,
    tab_enabled: dict[str, bool],
    margin_size: float,
) -> dict:
    """Build a fresh state dict — identical fields to legacy load_state defaults."""
    return {
        "balances": {tab: INITIAL_BALANCE for tab in TABS},
        "unrealized_pnls": {tab: 0.0 for tab in TABS},
        "open_positions": {},
        "pending_entry_orders": {},
        "position_registry": {},
        "history": [],
        "used_setups": [],
        "sync_issues": [],
        "error_events": [],
        "margin_history": [],
        "binance_income": {
            "realized_pnl": 0.0,
            "commission": 0.0,
            "funding": 0.0,
            "last_ts": int(datetime.now(timezone.utc).timestamp() * 1000),
        },
        "tab_enabled": tab_enabled,
        "long_short_balance_mode": "off",
        "trade_side_mode": "both",
        "max_positions_per_tab": MAX_POSITIONS_PER_TAB,
        "margin_size": margin_size,
        "leverage": LEVERAGE,
        "notional_size": NOTIONAL_SIZE,
        "symbol_scan_limit": SYMBOL_SCAN_LIMIT,
        "symbol_scan_limit_by_tab": default_tab17_scan_limit_by_tab(),
        "sltp_mode": SLTP_MODE,
        "circuit_breaker": False,
        "daily_loss_usd": 0.0,
        "daily_loss_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


def default_state_corrupt_recovery(base: dict) -> dict:
    """Corrupt-file recovery adds Recovered ledger rows (legacy load_state behavior)."""
    base = dict(base)
    base["unrealized_pnls"] = dict(base.get("unrealized_pnls") or {})
    base["balances"] = dict(base.get("balances") or {})
    base["unrealized_pnls"]["Recovered"] = 0.0
    base["balances"]["Recovered"] = 0.0
    return base
