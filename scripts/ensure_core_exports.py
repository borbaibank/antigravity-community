"""Ensure core.py re-exports symbols required by tests and leftover core helpers."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "bot" / "core.py"


def main() -> None:
    text = CORE.read_text(encoding="utf-8")
    changed = False

    if "from bot.feeds.klines import get_klines" not in text:
        needle = "from bot.engine.entry import ("
        if needle in text:
            text = text.replace(
                needle,
                "from bot.feeds.klines import get_klines\n\n" + needle,
                1,
            )
            changed = True
            print("Added get_klines import to core.py")

    if changed:
        CORE.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
