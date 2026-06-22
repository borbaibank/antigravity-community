"""Extract state accessor helpers from bot/core.py into bot/state/accessors.py."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "bot" / "core.py"
OUT = ROOT / "bot" / "state" / "accessors.py"

HEADER = '''"""State accessors — effective/normalize helpers extracted from bot.core."""

from __future__ import annotations

from datetime import datetime, timezone

from config import (
    BINANCE_CLOSE_HISTORY_ENABLED,
    BINANCE_TESTNET,
    INITIAL_BALANCE,
    LIVE_MODE,
    LEVERAGE,
    MAX_POSITIONS_PER_TAB,
    NOTIONAL_SIZE,
    SLTP_MODE,
    SLTP_MODES,
    SYMBOL_FILTER_DEFAULT_MIN_NET_PNL,
    SYMBOL_FILTER_DEFAULT_MIN_TRADES,
    SYMBOL_FILTER_DEFAULT_MIN_WIN_RATE,
    SYMBOL_FILTER_MODES,
    SYMBOL_FILTER_ROLLING_WINDOW,
    SYMBOL_SCAN_LIMIT,
    TAB17_BASE_UNIVERSE,
    TAB17_MAX_POS,
    TAB_TIMEFRAMES,
    TABS,
)


def _core():
    from bot import core
    return core


'''

CORE_ATTRS = (
    "SCAN_SYMBOLS",
    "MAX_POSITIONS_OPTIONS",
    "NOTIONAL_SIZE_OPTIONS",
    "LEVERAGE_OPTIONS",
    "SYMBOL_SCAN_OPTIONS",
    "_DEFAULT_MARGIN_SIZE",
    "STARTUP_ENABLED_TABS",
    "_BINANCE_CLOSE_HISTORY_CACHE",
)

CONFIG_VIA_CORE = (
    "BINANCE_CLOSE_HISTORY_ENABLED",
    "BINANCE_TESTNET",
    "INITIAL_BALANCE",
    "LIVE_MODE",
    "LEVERAGE",
    "MAX_POSITIONS_PER_TAB",
    "NOTIONAL_SIZE",
    "SLTP_MODE",
    "SLTP_MODES",
    "SYMBOL_FILTER_DEFAULT_MIN_NET_PNL",
    "SYMBOL_FILTER_DEFAULT_MIN_TRADES",
    "SYMBOL_FILTER_DEFAULT_MIN_WIN_RATE",
    "SYMBOL_FILTER_MODES",
    "SYMBOL_FILTER_ROLLING_WINDOW",
    "SYMBOL_SCAN_LIMIT",
    "TAB17_BASE_UNIVERSE",
    "TAB17_MAX_POS",
    "TAB_TIMEFRAMES",
    "TABS",
)

CHUNK_END_MARKERS = (
    "def _position_sl_is_local",
    "from bot.state.persistence import",
)


def _function_names(chunk: str) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(chunk).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
    return names


def _transform_chunk(chunk: str, local_funcs: set[str]) -> str:
    placeholders: dict[str, tuple[str, str, str]] = {}

    def stash_def(match: re.Match[str]) -> str:
        key = f"__DEF_{len(placeholders)}__"
        placeholders[key] = (match.group(1) or "", match.group(2), match.group(3))
        return key

    chunk = re.sub(
        r"^(async )?def ([a-zA-Z_]\w*)(\([^)]*\))(?:\s*->[^\n]+)?:\n",
        stash_def,
        chunk,
        flags=re.MULTILINE,
    )

    for attr in CORE_ATTRS + CONFIG_VIA_CORE:
        chunk = re.sub(rf"(?<!c\.)\b{re.escape(attr)}\b", f"c.{attr}", chunk)

    chunk = re.sub(r"(?<![a-zA-Z_])state(?![a-zA-Z_])", "c.state", chunk)

    def repl_helper(match: re.Match[str]) -> str:
        return f"c._{match.group(1)}("

    chunk = re.sub(
        r"(?<!def )(?<!async )(?<![.\w])_([a-zA-Z]\w*)\(",
        repl_helper,
        chunk,
    )

    for key, (async_kw, name, args) in placeholders.items():
        chunk = chunk.replace(key, f"{async_kw}def {name}{args}:\n    c = _core()\n")

    return chunk


def _collect_exports(chunk: str) -> list[str]:
    exports: list[str] = []
    for node in ast.parse(chunk).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            exports.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.startswith("_"):
                    exports.append(target.id)
    return exports


def _extract_chunks(lines: list[str]) -> str:
    idx_match = next(i for i, l in enumerate(lines) if l.startswith("def _match_float_option"))
    idx_prot = next(i for i, l in enumerate(lines) if l.startswith("def _position_sl_is_local"))
    idx_startup = next(i for i, l in enumerate(lines) if l.startswith("def _startup_tab_enabled"))
    idx_persist = next(
        i for i, l in enumerate(lines) if l.startswith("from bot.state.persistence import")
    )
    return "".join(lines[idx_match:idx_prot]) + "".join(lines[idx_startup:idx_persist])


def main() -> None:
    text = CORE.read_text(encoding="utf-8")
    if "from bot.state.accessors import" in text:
        print("Already extracted; accessors import present in core.py")
        return

    lines = text.splitlines(keepends=True)
    chunk = _extract_chunks(lines)
    local_funcs = _function_names(chunk)
    module = HEADER + _transform_chunk(chunk, local_funcs).rstrip() + "\n"
    OUT.write_text(module, encoding="utf-8")

    exports = _collect_exports(chunk)
    import_lines = ["from bot.state.accessors import ("]
    for name in exports:
        import_lines.append(f"    {name},")
    import_lines.append(")\n\n")
    import_block = "\n".join(import_lines)

    idx_match = next(i for i, l in enumerate(lines) if l.startswith("def _match_float_option"))
    idx_prot = next(i for i, l in enumerate(lines) if l.startswith("def _position_sl_is_local"))
    idx_startup = next(i for i, l in enumerate(lines) if l.startswith("def _startup_tab_enabled"))
    idx_persist = next(
        i for i, l in enumerate(lines) if l.startswith("from bot.state.persistence import")
    )

    new_lines = (
        lines[:idx_match]
        + [import_block]
        + lines[idx_prot:idx_startup]
        + lines[idx_persist:]
    )
    CORE.write_text("".join(new_lines), encoding="utf-8")
    print(f"Wrote {OUT} ({len(module.splitlines())} lines, {len(exports)} exports)")
    print(f"Removed accessor chunks from core.py")


if __name__ == "__main__":
    main()
