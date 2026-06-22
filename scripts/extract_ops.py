"""Extract telegram, error events, health monitor into bot/engine/ops.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from domain_extract import (
    build_import_block,
    collect_exports,
    function_names,
    slice_by_markers,
    transform_chunk,
)

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "bot" / "core.py"
OUT = ROOT / "bot" / "engine" / "ops.py"
IMPORT_CHECK = "from bot.engine.ops import"

HEADER = '''"""Telegram, error events, and health snapshot (extracted from bot.core)."""

from __future__ import annotations

from datetime import datetime, timezone


def _core():
    from bot import core
    return core


_ERROR_EVENTS_CAP = 200
_ERROR_EVENT_DEDUP_SEC = 120.0
_error_event_recent: dict[str, float] = {}


'''

CORE_ATTRS = (
    "_http_client",
    "_telegram_client",
    "_uds_connected",
    "_last_uds_connected_at",
    "_last_uds_error_at",
    "_last_sync_ok_at",
    "_last_sync_error_at",
    "_last_exchange_account_ok_at",
    "_last_exchange_account_error_at",
    "_last_price_ws_ok_at",
    "_last_scheduler_ok_at",
    "_last_watchdog_ok_at",
)

CONFIG_VIA_CORE = ("LIVE_MODE", "TELEGRAM_ENABLED", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")

ACCESSOR_VIA_CORE = ("_LOCAL_SLTP_POLICY_REASONS",)


def _extract_chunks(lines: list[str]) -> str:
    a, _, _ = slice_by_markers(
        lines, "def _is_tp_sl_exit_reason", "from bot.state.position_identity import"
    )
    b, _, _ = slice_by_markers(lines, "def _iso_age_seconds", "def _dt_to_ms")
    return a + b


def _remove_chunks(lines: list[str]) -> list[str]:
    _, s1, e1 = slice_by_markers(
        lines, "def _is_tp_sl_exit_reason", "from bot.state.position_identity import"
    )
    _, s2, e2 = slice_by_markers(lines, "def _iso_age_seconds", "def _dt_to_ms")
    if s2 > e1:
        return lines[:s1] + lines[e1:s2] + lines[e2:]
    return lines[:s1] + lines[e2:]


def main() -> None:
    text = CORE.read_text(encoding="utf-8")
    if IMPORT_CHECK in text:
        print("Already extracted.")
        return

    lines = text.splitlines(keepends=True)
    chunk = _extract_chunks(lines)
    local_funcs = function_names(chunk)
    module = HEADER + transform_chunk(
        chunk,
        local_funcs,
        core_attrs=CORE_ATTRS,
        config_via_core=CONFIG_VIA_CORE,
        accessor_via_core=ACCESSOR_VIA_CORE,
    ).rstrip() + "\n"
    OUT.write_text(module, encoding="utf-8")

    exports = collect_exports(chunk) + ["_ERROR_EVENTS_CAP", "_ERROR_EVENT_DEDUP_SEC", "_error_event_recent"]
    import_block = build_import_block("bot.engine.ops", exports)
    new_lines = _remove_chunks(lines)
    insert_at = next(
        i for i, l in enumerate(new_lines) if l.startswith("from bot.state.position_identity import")
    )
    new_lines = new_lines[:insert_at] + [import_block] + new_lines[insert_at:]
    CORE.write_text("".join(new_lines), encoding="utf-8")
    print(f"Wrote {OUT} ({len(module.splitlines())} lines, {len(exports)} exports)")


if __name__ == "__main__":
    main()
