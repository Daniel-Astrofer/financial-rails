"""Typed application errors mapped to stable HTTP and RPC failure responses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ApiError(Exception):
    """Application failure carrying the HTTP and JSON fields returned to callers."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Create an API error with optional structured response details.

        ``details`` defaults to an empty mapping; ``message`` is also the base
        exception text used by logging and Flask error handling.
        """
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass
class RpcError(Exception):
    """Bitcoin Core RPC failure retaining method, node code, and HTTP mapping.

    Attributes:
        method: RPC operation that failed.
        rpc_code: Numeric error code returned by Bitcoin Core, when available.
        message: Bounded provider message safe for API mapping.
        status_code: HTTP status to use when no more specific RPC mapping applies.
    """

    method: str
    rpc_code: int | None
    message: str
    status_code: int = 502

    def __str__(self) -> str:
        """Format a concise node-operation failure for the API response."""
        return f"Bitcoin Core RPC {self.method} failed: {self.message}"


def rpc_error_to_api_error(error: RpcError) -> ApiError:
    """Map known Bitcoin Core RPC codes to stable application codes/statuses.

    Wallet lookup, insufficient-funds, rejected-request, and node-readiness
    failures receive dedicated API mappings; unknown node errors retain the
    status carried by ``error`` and use the generic RPC error code.
    """
    code = "BITCOIN_RPC_ERROR"
    status = error.status_code
    if error.rpc_code in {-18, -19}:
        code = "BITCOIN_WALLET_NOT_FOUND"
        status = 404
    elif error.rpc_code in {-4, -6}:
        code = "BITCOIN_WALLET_INSUFFICIENT_FUNDS"
        status = 409
    elif error.rpc_code in {-5, -8, -22}:
        code = "BITCOIN_RPC_REJECTED_REQUEST"
        status = 400
    elif error.rpc_code in {-28, -9}:
        code = "BITCOIN_CORE_NOT_READY"
        status = 503
    return ApiError(status, code, str(error), {"rpcCode": error.rpc_code, "method": error.method})
