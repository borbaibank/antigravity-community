"""Bot state layer."""

from bot.state.accessors import (
    _effective_sltp_mode,
    _normalize_tab_enabled,
    _startup_tab_enabled,
)
from bot.state.persistence import load_state, save_state
from bot.state.position_identity import (
    _position_side_from_state,
    _position_side_name,
    _strategy_client_id,
)
from bot.state.schema import default_state, default_state_corrupt_recovery

__all__ = [
    "default_state",
    "default_state_corrupt_recovery",
    "load_state",
    "save_state",
    "_effective_sltp_mode",
    "_normalize_tab_enabled",
    "_position_side_from_state",
    "_position_side_name",
    "_startup_tab_enabled",
    "_strategy_client_id",
]