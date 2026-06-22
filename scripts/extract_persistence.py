"""Extract load_state/save_state from bot/core.py into bot/state/persistence.py."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "bot" / "core.py"
OUT = ROOT / "bot" / "state" / "persistence.py"

HEADER = '''"""Paper state load/save — behavior-preserving extract from bot.core."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone

from bot.state.schema import default_state, default_state_corrupt_recovery
from config import (
    INITIAL_BALANCE,
    LIVE_MODE,
    MAX_POSITIONS_PER_TAB,
    SLTP_MODE,
    SYMBOL_SCAN_LIMIT,
    TAB17_BASE_UNIVERSE,
    TABS,
)


def _core():
    from bot import core
    return core


'''

IMPORT_BLOCK = '''from bot.state.persistence import (
    _state_write_guard_allows_save,
    _state_write_lock,
    _write_state,
    load_state,
    save_state,
)

'''

LOCAL_FUNCS = frozenset({
    "load_state",
    "_write_state",
    "_state_write_guard_allows_save",
    "save_state",
})

CORE_ATTRS = (
    "_DEFAULT_MARGIN_SIZE",
    "MAX_POSITIONS_OPTIONS",
    "SYMBOL_SCAN_OPTIONS",
    "_TAB_STATS_VERSION",
    "_SYMBOL_STATS_VERSION",
    "STATE_FILE",
    "STATE_ARCHIVE_DIR",
)


def _transform_chunk(chunk: str) -> str:
    chunk = re.sub(r"^\s*global state\s*\n", "", chunk, flags=re.MULTILINE)

    placeholders: dict[str, tuple[str, str, str]] = {}

    def stash_def(match: re.Match[str]) -> str:
        key = f"__DEF_{len(placeholders)}__"
        placeholders[key] = (match.group(1) or "", match.group(2), match.group(3))
        return key

    chunk = re.sub(
        r"^(async )?def (load_state|_write_state|_state_write_guard_allows_save|save_state)(\([^)]*\))(?:\s*->[^\n]+)?:\n",
        stash_def,
        chunk,
        flags=re.MULTILINE,
    )

    for attr in CORE_ATTRS:
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

    return chunk


def main() -> None:
    text = CORE.read_text(encoding="utf-8")
    if "from bot.state.persistence import" in text:
        print("Already extracted; run scripts/restore_core.py first.")
        return

    lines = text.splitlines(keepends=True)
    start = next(i for i, l in enumerate(lines) if l.startswith("def load_state("))
    end = next(i for i, l in enumerate(lines) if l.startswith("def _is_tp_sl_exit_reason"))
    chunk = "".join(lines[start:end])
    module = HEADER + _transform_chunk(chunk).rstrip() + "\n"
    OUT.write_text(module, encoding="utf-8")

    new_lines = lines[:start] + [IMPORT_BLOCK, "\n"] + lines[end:]
    CORE.write_text("".join(new_lines), encoding="utf-8")
    print(f"Wrote {OUT} ({len(module.splitlines())} lines)")
    print(f"Removed lines {start + 1}-{end} from core.py")


if __name__ == "__main__":
    main()
