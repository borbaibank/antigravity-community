"""Antigravity multi-strategy futures bot — entry point.

``import server`` aliases ``bot.core`` (single namespace for tests + runtime).
"""
import sys

import uvicorn
from bot import core

sys.modules[__name__] = core

if __name__ == "__main__":
    if not core._acquire_single_instance_lock():
        print("[Startup] Another server.py instance is already running; exiting.")
        raise SystemExit(1)

    uvicorn.run(
        core.app,
        host="0.0.0.0",
        port=core.DASHBOARD_PORT,
        reload=False,
        log_config=None,
        ws_ping_interval=None,
        ws_ping_timeout=None,
        ws_max_size=16 * 1024 * 1024,
    )
