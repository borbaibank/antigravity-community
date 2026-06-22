"""Tab evaluator registry — data-driven dispatch for scan_candle_signals."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import strategies

from bot.engine.premium_hooks import apply_premium_evaluators
from bot.state.accessors import _effective_trade_side_mode

# (tab_name, evaluator, post_process) — order matches legacy scan_candle_signals 100%
# post_process: None | "strip_swing" | "tab17_momentum"
# Tab11–Tab18 are registered by antigravity_pro when installed.
TAB_EVALUATORS_4H: list[tuple[str, Callable[..., dict | None], str | None]] = [
    ("Tab1", strategies.evaluate_tab1_ema4h, None),
    ("Tab2", strategies.evaluate_tab2_ema_1h, None),
    ("Tab3", strategies.evaluate_tab3_smc260, None),
    ("Tab4", strategies.evaluate_tab4_ote, "strip_swing"),
    ("Tab6", strategies.evaluate_tab6_squeeze_1h, None),
    ("Tab7", strategies.evaluate_tab7_cci_1h, None),
]

TAB_EVALUATORS_1H: list[tuple[str, Callable[..., dict | None], str | None]] = [
    ("Tab5", strategies.evaluate_tab5_rsi_divergence_1h, None),
    ("Tab8", strategies.evaluate_tab8_three_soldiers_crows, None),
    ("Tab9", strategies.evaluate_tab9_impulse_move_continuation, None),
    ("Tab10", strategies.evaluate_tab10_vol_range_expansion_spike, None),
]

_PREMIUM_EVALUATORS_APPLIED = False


def finalize_premium_evaluators() -> None:
    """Extend evaluator lists after antigravity_pro registers hooks (idempotent)."""
    global _PREMIUM_EVALUATORS_APPLIED
    if _PREMIUM_EVALUATORS_APPLIED:
        return
    apply_premium_evaluators(TAB_EVALUATORS_1H, TAB_EVALUATORS_4H)
    _PREMIUM_EVALUATORS_APPLIED = True


def apply_signal_post_process(sig: dict | None, post: str | None) -> dict | None:
    if not sig or not post:
        return sig
    if post == "strip_swing":
        sig = dict(sig)
        sig.pop("swing_low", None)
        sig.pop("swing_high", None)
        return sig
    return sig


def _allowed_side_for_eval(tab: str) -> str | None:
    """Map trade_side_mode to a single allowed side for strategy evaluators."""
    mode = _effective_trade_side_mode(tab)
    if mode == "long_only":
        return "Long"
    if mode == "short_only":
        return "Short"
    return None


def evaluate_tab_signal(
    tab: str,
    evaluator: Callable[..., dict | None],
    post: str | None,
    df: Any,
) -> dict | None:
    kwargs: dict[str, str] = {}
    allowed = _allowed_side_for_eval(tab)
    if allowed and "allowed_side" in inspect.signature(evaluator).parameters:
        kwargs["allowed_side"] = allowed
    sig = evaluator(df.copy(), **kwargs)
    sig = apply_signal_post_process(sig, post)
    if sig and allowed and sig.get("side") != allowed:
        return None
    return sig
