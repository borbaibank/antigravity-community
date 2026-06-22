"""Rebuild bot/core.py from git + all domain extraction scripts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (
    "restore_core.py",
    "extract_persistence.py",
    "extract_accessors.py",
    "extract_position_identity.py",
    "extract_protection.py",
    "extract_ops.py",
    "extract_batch_domains.py",
    "extract_phase2_domains.py",
    "extract_phase3_domains.py",
    "extract_api.py",
    "ensure_core_exports.py",
)


def main() -> None:
    for name in SCRIPTS:
        path = ROOT / "scripts" / name
        print(f"=== {name} ===")
        subprocess.run([sys.executable, str(path)], cwd=ROOT, check=True)
    print("Done — run unittest to verify.")


if __name__ == "__main__":
    main()
