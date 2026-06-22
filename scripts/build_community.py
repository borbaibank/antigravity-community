#!/usr/bin/env python3
"""Build Antigravity Community edition (Tab1–Tab10 only) into dist/community/.

Run from repo root::

    python scripts/build_community.py

Output: ``dist/community/`` — no ``antigravity_pro/``, no premium tabs in config/UI.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "community"

COMMUNITY_TABS = [f"Tab{i}" for i in range(1, 11)]
PREMIUM_TABS = {f"Tab{i}" for i in range(11, 19)}

SKIP_COPY = {
    "antigravity_pro",
    "dist",
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "paper_state.json",
    ".env",
    "archive",
    "cache",
}

COMMUNITY_LICENSE = """\
MIT License

Copyright (c) 2026 Antigravity

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

COMMUNITY_README = """\
# Antigravity Community Edition

Multi-strategy Binance USDT-M Futures bot with **Tab1–Tab10** (self-hosted).

## Strategies included

| Tab | Timeframe | Strategy |
|-----|-----------|----------|
| Tab1 | 4h | EMA Pullback |
| Tab2 | 4h | EMA Cross + ATR |
| Tab3 | 4h | SMC Order Block |
| Tab4 | 4h | Premium/Discount OTE |
| Tab5 | 1h | RSI Divergence |
| Tab6 | 4h | BB/KC Squeeze |
| Tab7 | 4h | CCI |
| Tab8 | 1h | Three Soldiers/Crows |
| Tab9 | 1h | Impulse Continuation |
| Tab10 | 1h | Volume Range Expansion |

## Premium (Tab11–Tab18)

Volume / momentum strategies (**Tab11–Tab18**) are sold separately as **Antigravity Pro**.
Install the Pro add-on package alongside this edition to unlock premium tabs.

## Quick start

```powershell
python -m venv .venv
.\\.venv\\Scripts\\pip install -r requirements.txt
copy .env.example .env
python server.py
```

Dashboard: http://localhost:6000

## License

MIT — see `LICENSE`. Pro strategies are under a separate commercial license.
"""


def copy_tree() -> None:
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)

    for src in ROOT.iterdir():
        if src.name in SKIP_COPY:
            continue
        dest = DIST / src.name
        if src.is_dir():
            shutil.copytree(
                src,
                dest,
                ignore=shutil.ignore_patterns(
                    "__pycache__",
                    "*.pyc",
                    "paper_state.json",
                    ".env",
                    "server.lock",
                ),
            )
        else:
            shutil.copy2(src, dest)

    pro_dir = DIST / "antigravity_pro"
    if pro_dir.exists():
        shutil.rmtree(pro_dir)


def strip_config() -> None:
    path = DIST / "config.py"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    out: list[str] = []
    in_tf = False
    skip_comment_block = False
    i = 0

    while i < len(lines):
        line = lines[i]

        if line.startswith("STARTUP_ENABLED_TABS"):
            out.append("STARTUP_ENABLED_TABS = frozenset({\n")
            out.append('    "Tab3",\n')
            out.append("})\n")
            i += 1
            while i < len(lines) and "})" not in lines[i]:
                i += 1
            i += 1
            continue

        if line.startswith("TAB_TIMEFRAMES = {"):
            in_tf = True
            out.append(line)
            i += 1
            continue
        if in_tf:
            if any(f'"{tab}"' in line for tab in PREMIUM_TABS):
                i += 1
                continue
            out.append(line)
            if line.strip() == "}":
                in_tf = False
            i += 1
            continue

        if re.match(r"^# --- Tab (1[1-8])", line):
            skip_comment_block = True
            i += 1
            continue
        if skip_comment_block:
            if line.startswith("# --- Tab") and not re.match(r"^# --- Tab (1[1-8])", line):
                skip_comment_block = False
            elif not line.startswith("#") and line.strip():
                skip_comment_block = False
            else:
                i += 1
                continue

        if re.match(r"^TAB1[1-8]_", line):
            i += 1
            continue

        out.append(line)
        i += 1

    path.write_text("".join(out), encoding="utf-8")


def strip_position_identity() -> None:
    path = DIST / "bot" / "state" / "position_identity.py"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    out: list[str] = []
    for line in lines:
        if re.match(r'\s+"Tab1[1-8]":', line):
            continue
        out.append(line)
    path.write_text("".join(out), encoding="utf-8")


def _tab_list_js(tabs: list[str]) -> str:
    return ",".join(f"'{t}'" for t in tabs)


def strip_index_html() -> None:
    path = DIST / "static" / "index.html"
    text = path.read_text(encoding="utf-8")

    for i in range(11, 19):
        text = re.sub(
            rf'\n\s*<button class="tab-btn" data-filter="Tab{i}"[^>]*>.*?</button>',
            "",
            text,
            flags=re.DOTALL,
        )
        text = re.sub(rf"\n\s*'Tab{i}': '.*?',", "", text, flags=re.DOTALL)

    text = re.sub(
        r"const STRAT_CURVE_TABS = \[[\s\S]*?\];",
        "const STRAT_CURVE_TABS = [\n"
        f"            {_tab_list_js(COMMUNITY_TABS)},\n"
        "            'SafeGuard', 'Recovered',\n"
        "        ];",
        text,
        count=1,
    )

    comm = _tab_list_js(COMMUNITY_TABS)
    text = re.sub(
        r"const TAB_LIST = \[[^\]]+\];",
        f"const TAB_LIST = [{comm}];",
        text,
        count=1,
    )

    text = re.sub(
        r"const stratRows = \[[^\]]+\];",
        f"const stratRows = [{comm},'SafeGuard','Recovered'];",
        text,
        count=1,
    )

    banner = """
            <div id="community-pro-banner" style="margin:0 0 12px;padding:10px 14px;background:#0d2818;border:1px solid #238636;border-radius:8px;font-size:13px;color:#aff5b4;">
                <strong>Community Edition</strong> — Tab1–Tab10 included.
                Tab11–Tab18 require <strong>Antigravity Pro</strong> (commercial add-on).
            </div>
"""
    text = text.replace(
        '<div class="tab-bar">',
        banner + '\n            <div class="tab-bar">',
        1,
    )

    path.write_text(text, encoding="utf-8")


def strip_premium_tests() -> None:
    path = DIST / "tests" / "test_volume_strategies.py"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    text = re.sub(
        r"\n    def test_(tab1[1-8]|volume_clone|tab17|tab18|4h_clone)[^\n]*\n(?:.*\n)*?(?=\n    def |\n\nclass |\Z)",
        "\n",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\ntry:\n    from antigravity_pro.*?\nexcept ImportError:\n    pass\n\n",
        "\n",
        text,
        flags=re.DOTALL,
    )
    # Remove Tab11+ timeframe assertions
    text = re.sub(
        r"(\n        self\.assertEqual\(config\.TAB_TIMEFRAMES\[\"Tab10\"\].*?\n)"
        r"(        self\.assertEqual\(config\.TAB_TIMEFRAMES\[\"Tab1[1-8]\"].*?\n)+",
        r"\1",
        text,
    )
    text = re.sub(
        r"\n    def test_volume_clone_tabs_run_on_4h\(self\):.*?(?=\n    def |\Z)",
        "\n",
        text,
        flags=re.DOTALL,
    )
    path.write_text(text, encoding="utf-8")


def remove_premium_scripts() -> None:
    for name in (
        "scripts/import_tab18_backtest_history.py",
    ):
        p = DIST / name
        if p.exists():
            p.unlink()


def write_license_and_readme() -> None:
    (DIST / "LICENSE").write_text(COMMUNITY_LICENSE, encoding="utf-8")
    readme_src = ROOT / "docs" / "README_COMMUNITY_PUBLIC.md"
    if readme_src.exists():
        (DIST / "README.md").write_text(readme_src.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        (DIST / "README.md").write_text(COMMUNITY_README, encoding="utf-8")


def verify_dist() -> int:
    py = sys.executable
    print("Verifying dist/community ...")
    steps = [
        [py, "-m", "py_compile", "server.py", "config.py", "strategies.py"],
        [py, "-m", "unittest", "tests.test_volume_strategies", "-q"],
    ]
    for cmd in steps:
        print(" ", " ".join(cmd))
        result = subprocess.run(cmd, cwd=DIST, capture_output=True, text=True)
        if result.returncode != 0:
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
            return result.returncode
    print(result.stdout)
    return 0


def main() -> int:
    print(f"Building community edition -> {DIST}")
    copy_tree()
    strip_config()
    strip_position_identity()
    strip_index_html()
    strip_premium_tests()
    remove_premium_scripts()
    write_license_and_readme()
    print("Build complete.")
    return verify_dist()


if __name__ == "__main__":
    sys.exit(main())
