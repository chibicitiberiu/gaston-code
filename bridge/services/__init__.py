"""Stateless bridge services with dependency injection.

Built in topological order (L1..L8) with explicit dependencies flowing
through constructors, and state scoped to GuestSession flowing through parameters.
"""

from bridge.services.config import BridgeConfig
from bridge.services.options import (
    COMMAND_MENU,
    EFFORT_LABEL,
    EFFORT_ORDER,
    MODE_CAVEAT,
    MODE_LABEL,
    MODE_ORDER,
    OptionsService,
)
from bridge.services.presentation import PresentationService
from bridge.services.workspace import WorkspaceService

__all__ = [
    "BridgeConfig",
    "OptionsService",
    "PresentationService",
    "WorkspaceService",
    "COMMAND_MENU",
    "EFFORT_LABEL",
    "EFFORT_ORDER",
    "MODE_CAVEAT",
    "MODE_LABEL",
    "MODE_ORDER",
]
