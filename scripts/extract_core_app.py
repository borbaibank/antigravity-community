"""Extract bot/core.py and bot/app.py from server.py."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
lines = (ROOT / "server.py").read_text(encoding="utf-8").splitlines(keepends=True)
state_i = next(i for i, l in enumerate(lines) if "# --- PAPER TRADING STATE ---" in l)
fastapi_i = next(i for i, l in enumerate(lines) if "# --- FASTAPI APP ---" in l)
main_i = next(i for i, l in enumerate(lines) if l.startswith("if __name__"))
core = "".join(lines[state_i:fastapi_i])
api = "".join(lines[fastapi_i:main_i])
main_block = "".join(lines[main_i:])

CORE_HEADER = '''"""Trading engine core — extracted from server.py (behavior-preserving)."""
from __future__ import annotations

''' + "".join(lines[1:64]) + '''
from bot import logging_setup as _logmod

_log_buffer = _logmod._log_buffer
LOG_ARCHIVE_DIR = _logmod.LOG_ARCHIVE_DIR
_log_entry_open = _logmod._log_entry_open
_log_exit_close = _logmod._log_exit_close
_log_price = _logmod._log_price
_exit_move_pct_from_entry = _logmod._exit_move_pct_from_entry
_exit_target_slip_from_fill = _logmod._exit_target_slip_from_fill
_utc_log_stamp = _logmod._utc_log_stamp
_configure_library_loggers = _logmod._configure_library_loggers

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

'''

(ROOT / "bot" / "core.py").write_text(CORE_HEADER + core, encoding="utf-8")
print("bot/core.py", len((CORE_HEADER + core).splitlines()), "lines")

APP_HEADER = '"""FastAPI application — extracted from server.py."""\nfrom bot.core import *\n\n'
(ROOT / "bot" / "app.py").write_text(APP_HEADER + api, encoding="utf-8")
print("bot/app.py", len((APP_HEADER + api).splitlines()), "lines")

# Thin server.py facade
SERVER_FACADE = '''"""Antigravity multi-strategy futures bot — entry point."""
from bot.core import *
from bot.app import app

''' + main_block

(ROOT / "server.py").write_text(SERVER_FACADE, encoding="utf-8")
print("server.py", len(SERVER_FACADE.splitlines()), "lines")
