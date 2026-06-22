"""Optional premium tab hooks — populated by ``antigravity_pro`` when installed."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_PREMIUM_LOADED = False
_PREMIUM_TABS: frozenset[str] = frozenset()
_EVALUATORS_1H: list[tuple[str, Callable[..., dict | None], str | None]] = []
_EVALUATORS_4H: list[tuple[str, Callable[..., dict | None], str | None]] = []
_TAB_MAX_POSITIONS: dict[str, int] = {}
_TAB17_MOMENTUM_UNIVERSE: Callable[..., dict[str, float]] | None = None
_SORT_ENTRY_CANDIDATES: Callable[[str, list[dict]], None] | None = None


def premium_loaded() -> bool:
    return _PREMIUM_LOADED


def premium_tabs() -> frozenset[str]:
    return _PREMIUM_TABS


def register_premium(
    *,
    tabs: frozenset[str],
    evaluators_1h: list[tuple[str, Callable[..., dict | None], str | None]],
    evaluators_4h: list[tuple[str, Callable[..., dict | None], str | None]],
    tab_max_positions: dict[str, int] | None = None,
    tab17_momentum_universe: Callable[..., dict[str, float]] | None = None,
    sort_entry_candidates: Callable[[str, list[dict]], None] | None = None,
) -> None:
    global _PREMIUM_LOADED, _PREMIUM_TABS
    _PREMIUM_LOADED = True
    _PREMIUM_TABS = frozenset(tabs)
    _EVALUATORS_1H.extend(evaluators_1h)
    _EVALUATORS_4H.extend(evaluators_4h)
    if tab_max_positions:
        _TAB_MAX_POSITIONS.update(tab_max_positions)
    global _TAB17_MOMENTUM_UNIVERSE, _SORT_ENTRY_CANDIDATES
    if tab17_momentum_universe is not None:
        _TAB17_MOMENTUM_UNIVERSE = tab17_momentum_universe
    if sort_entry_candidates is not None:
        _SORT_ENTRY_CANDIDATES = sort_entry_candidates


def apply_premium_evaluators(
    evaluators_1h: list[tuple[str, Callable[..., dict | None], str | None]],
    evaluators_4h: list[tuple[str, Callable[..., dict | None], str | None]],
) -> None:
    evaluators_1h.extend(_EVALUATORS_1H)
    evaluators_4h.extend(_EVALUATORS_4H)


def tab_max_positions_override(tab_name: str) -> int | None:
    return _TAB_MAX_POSITIONS.get(tab_name)


def build_tab17_momentum_universe(symbols: list[str], results: list) -> dict[str, float]:
    if _TAB17_MOMENTUM_UNIVERSE is None:
        return {}
    return _TAB17_MOMENTUM_UNIVERSE(symbols, results)


def sort_entry_candidates(tab_name: str, candidates: list[dict]) -> None:
    if _SORT_ENTRY_CANDIDATES is not None:
        _SORT_ENTRY_CANDIDATES(tab_name, candidates)
