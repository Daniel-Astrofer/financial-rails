"""Application ports used to wire the Bitcoin capability to adapters."""
from __future__ import annotations

from typing import Any, Protocol


class BitcoinSettingsPort(Protocol):
    """Configuration values consumed by the application capability."""

    def __getattr__(self, name: str) -> Any:
        """Resolve a declared setting by name for structural configuration adapters."""
        ...


class BitcoinNodePort(Protocol):
    """RPC boundary used by application services to query or mutate Bitcoin Core."""

    def call(self, method: str, params: list[Any] | None = None, wallet: str | None = None) -> Any:
        """Invoke one Bitcoin Core JSON-RPC method.

        Args:
            method: RPC method name, such as ``getblockchaininfo``.
            params: Positional JSON values passed to that RPC method.
            wallet: Optional wallet-scoped endpoint name; ``None`` uses the node endpoint.

        Returns:
            The decoded JSON-RPC result. Adapter-specific RPC and transport errors propagate.
        """
        ...


class IdempotencyPort(Protocol):
    """Persistence port kept structural for the existing store contract."""

    def __getattr__(self, name: str) -> Any:
        """Resolve an idempotency operation exposed by the configured persistence adapter."""
        ...
