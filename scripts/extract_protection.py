"""Extract SL/TP protection helpers into bot/engine/protection.py."""

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
OUT = ROOT / "bot" / "engine" / "protection.py"
IMPORT_CHECK = "from bot.engine.protection import"

HEADER = '''"""Entry/position SL-TP protection policy (extracted from bot.core)."""

from __future__ import annotations


def _core():
    from bot import core
    return core


'''

CORE_ATTRS = ()
CONFIG_VIA_CORE = ("LIVE_MODE",)

ACCESSOR_VIA_CORE = (
    "_MAINNET_LOCAL_SLTP_REASON",
    "_BINANCE_EXCHANGE_REASON",
    "_HYBRID_SLTP_REASON",
    "_FALLBACK_LOCAL_REASON",
)


def main() -> None:
    text = CORE.read_text(encoding="utf-8")
    if IMPORT_CHECK in text:
        print("Already extracted.")
        return

    lines = text.splitlines(keepends=True)
    chunk, start, end = slice_by_markers(
        lines, "def _position_sl_is_local", "from bot.state.persistence import"
    )
    local_funcs = function_names(chunk)
    module = HEADER + transform_chunk(
        chunk, local_funcs,
        config_via_core=CONFIG_VIA_CORE,
        accessor_via_core=ACCESSOR_VIA_CORE,
    ).rstrip() + "\n"
    OUT.write_text(module, encoding="utf-8")

    exports = collect_exports(chunk)
    import_block = build_import_block("bot.engine.protection", exports)
    CORE.write_text("".join(lines[:start] + [import_block] + lines[end:]), encoding="utf-8")
    print(f"Wrote {OUT} ({len(module.splitlines())} lines, {len(exports)} exports)")


if __name__ == "__main__":
    main()
