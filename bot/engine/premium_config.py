"""Optional premium config — safe when Tab11–18 constants are absent (community build)."""

from __future__ import annotations

import config


def tab17_base_universe() -> int | None:
    value = getattr(config, "TAB17_BASE_UNIVERSE", None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def default_tab17_scan_limit_by_tab() -> dict[str, int]:
    cap = tab17_base_universe()
    tabs = getattr(config, "TABS", [])
    if cap is None or "Tab17" not in tabs:
        return {}
    return {"Tab17": cap}
