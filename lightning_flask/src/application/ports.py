"""Ports used to wire the Lightning capability to its adapters."""
from __future__ import annotations

from typing import Any, Protocol


class LightningNodePort(Protocol):
    """RPC boundary for the configured Lightning node and its channel operations."""

    def get_info(self) -> dict[str, Any]:
        """Return the node identity, version, and readiness details from LND ``GetInfo``."""
        ...

    def __getattr__(self, name: str) -> Any:
        """Resolve an additional Lightning RPC supported by the configured adapter."""
        ...


class CohesionPort(Protocol):
    """Structural port for idempotency and sanitized cohesion persistence."""

    def __getattr__(self, name: str) -> Any:
        """Resolve a persistence operation used for idempotency and request cohesion."""
        ...
