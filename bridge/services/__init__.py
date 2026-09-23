"""Stateless bridge services with dependency injection.

Built in topological order (L1..L8) with explicit dependencies flowing
through constructors, and state scoped to GuestSession flowing through parameters.
"""

from bridge.services.config import BridgeConfig

__all__ = ["BridgeConfig"]
