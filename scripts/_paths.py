"""Project root paths for scripts/ utilities (run from repo root or scripts/)."""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(ROOT, "paper_state.json")
CACHE_DIR = os.path.join(ROOT, "cache")
ARCHIVE_DIR = os.path.join(ROOT, "archive")
STATE_ARCHIVE_DIR = os.path.join(ARCHIVE_DIR, "paper_state")
LOG_ARCHIVE_DIR = os.path.join(ARCHIVE_DIR, "server_logs")


def ensure_archive_dirs() -> None:
    os.makedirs(STATE_ARCHIVE_DIR, exist_ok=True)
    os.makedirs(LOG_ARCHIVE_DIR, exist_ok=True)
