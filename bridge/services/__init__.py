"""Stateless bridge services with dependency injection.

Built in topological order (L1..L8) with explicit dependencies flowing
through constructors, and state scoped to GuestSession flowing through parameters.
"""

from bridge.services.attachment import AttachmentService
from bridge.services.config import BridgeConfig
from bridge.services.connection import ConnectionService
from bridge.services.coordinator import SessionCoordinator
from bridge.services.guest_exec import GuestExecService
from bridge.services.options import (
    COMMAND_MENU,
    EFFORT_LABEL,
    EFFORT_ORDER,
    MODE_CAVEAT,
    MODE_LABEL,
    MODE_ORDER,
    OptionsService,
)
from bridge.services.permission import PermissionService
from bridge.services.presentation import PresentationService
from bridge.services.session import SessionService
from bridge.services.sync import SyncService
from bridge.services.turn_runner import TurnRunner, turn_verdict
from bridge.services.workspace import WorkspaceService

__all__ = [
    "COMMAND_MENU",
    "EFFORT_LABEL",
    "EFFORT_ORDER",
    "MODE_CAVEAT",
    "MODE_LABEL",
    "MODE_ORDER",
    "AttachmentService",
    "BridgeConfig",
    "ConnectionService",
    "GuestExecService",
    "OptionsService",
    "PermissionService",
    "PresentationService",
    "SessionCoordinator",
    "SessionService",
    "SyncService",
    "TurnRunner",
    "WorkspaceService",
    "turn_verdict",
]
