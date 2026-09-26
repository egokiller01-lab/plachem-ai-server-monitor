from __future__ import annotations

from collections.abc import Callable
from typing import Any

from command_center_client import COMMAND_CENTER_MODE, LEGACY_MODE


def execute_war_room_task(
    mode: str,
    *,
    command_center: Callable[[], Any],
    legacy: Callable[[], Any],
) -> Any:
    """Select one explicit execution path; never silently cross the boundary."""
    if mode == COMMAND_CENTER_MODE:
        # Errors remain errors. In particular, do not invoke legacy after a
        # Command Center transport or execution failure.
        return command_center()
    if mode == LEGACY_MODE:
        return legacy()
    raise ValueError(f"unsupported execution mode: {mode}")
