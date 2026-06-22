"""Antigravity trading bot package.

Layout:
- ``core`` — hub: startup, circuit breaker, protection validation, recovery helpers
- ``logging_setup`` — terminal/file logging
- ``state/`` — schema, persistence, accessors, position_identity
- ``engine/`` — entry, exit, sync, signals, protection, ops, history, pnl
- ``feeds/`` — market, klines, ws
- ``scheduler/`` — background loops
- ``api/web.py`` — FastAPI app + REST routes
"""

from bot import core

__all__ = ["core"]
