"""Extract hedge/position identity helpers into bot/state/position_identity.py."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "bot" / "core.py"
OUT = ROOT / "bot" / "state" / "position_identity.py"

HEADER = '''"""Hedge leg identity, client order IDs, and position registry."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

import binance_live
from config import TABS


def _core():
    from bot import core
    return core


'''

CORE_ATTRS = (
    "state",
    "_SYNC_ENTRY_GRACE_SEC",
)

CONFIG_VIA_CORE = ("TABS",)

MODULE_LOCAL_NAMES = (
    "_recent_bot_close_fills",
    "_BOT_CLOSE_FILL_TTL_SEC",
    "STRATEGY_LABELS",
    "TG_OPEN",
    "TG_ENTRY",
    "TG_TP_CLOSE",
    "TG_PROFIT_CLOSE",
    "TG_LOSS_CLOSE",
)


def _function_names(chunk: str) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(chunk).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
    return names


def _transform_chunk(chunk: str, local_funcs: set[str]) -> str:
    chunk = re.sub(r"^\s*global state\s*\n", "", chunk, flags=re.MULTILINE)

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
        if attr in MODULE_LOCAL_NAMES:
            continue
        chunk = re.sub(rf"(?<![.\w]){re.escape(attr)}(?![.\w])", f"c.{attr}", chunk)

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

    chunk = re.sub(
        r'grace_sec = getattr\(c, "c\._SYNC_ENTRY_GRACE_SEC", 180\.0\)',
        "grace_sec = c._SYNC_ENTRY_GRACE_SEC",
        chunk,
    )
    chunk = re.sub(
        r'grace_sec = globals\(\)\.get\("_SYNC_ENTRY_GRACE_SEC", 180\.0\)',
        "grace_sec = c._SYNC_ENTRY_GRACE_SEC",
        chunk,
    )

    return chunk


def _collect_exports(chunk: str) -> list[str]:
    exports: list[str] = []
    for node in ast.parse(chunk).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            exports.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    exports.append(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            exports.append(node.target.id)
    return exports


def main() -> None:
    text = CORE.read_text(encoding="utf-8")
    if "from bot.state.position_identity import" in text:
        print("Already extracted.")
        return

    lines = text.splitlines(keepends=True)
    start = next(i for i, l in enumerate(lines) if l.startswith("def _algo_id("))
    end = next(i for i, l in enumerate(lines) if l.startswith("def _iso_age_seconds("))
    chunk = "".join(lines[start:end])
    local_funcs = _function_names(chunk)
    module = HEADER + _transform_chunk(chunk, local_funcs).rstrip() + "\n"
    OUT.write_text(module, encoding="utf-8")

    exports = _collect_exports(chunk)
    import_lines = ["from bot.state.position_identity import ("]
    for name in exports:
        import_lines.append(f"    {name},")
    import_lines.append(")\n\n")
    import_block = "\n".join(import_lines)

    new_lines = lines[:start] + [import_block] + lines[end:]
    CORE.write_text("".join(new_lines), encoding="utf-8")
    print(f"Wrote {OUT} ({len(module.splitlines())} lines, {len(exports)} exports)")


if __name__ == "__main__":
    main()
