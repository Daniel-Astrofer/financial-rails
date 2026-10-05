"""JSON-RPC transport and domain mappings for Bitcoin Core wallet operations."""

from __future__ import annotations

import itertools
from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth

from src.config.settings import AppConfig
from src.application.errors import RpcError


class BitcoinRPCClient:
    """JSON-RPC transport for Bitcoin Core with bounded connection pooling."""

    def __init__(self, config: AppConfig, session: requests.Session | None = None) -> None:
        """Configure Basic authentication, pool sizes, and request-ID sequencing.

        An injected session is retained for controlled testing or custom
        transports; otherwise this client creates and owns a new session.
        """
        self._config = config
        self._session = session or requests.Session()
        adapter = HTTPAdapter(
            pool_connections=config.rpc_pool_size,
            pool_maxsize=config.rpc_pool_size,
            max_retries=0,
        )
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)
        self._auth = HTTPBasicAuth(config.rpc_user, config.rpc_password)
        self._ids = itertools.count(1)

    def call(self, method: str, params: list[Any] | None = None, wallet: str | None = None) -> Any:
        """Issue one RPC call and return its result, mapping transport/node failures.

        Timeouts become 504 errors, unreachable endpoints become 503 errors,
        and JSON-RPC failures retain the node code for application mapping.
        """
        url = self._url(wallet)
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": params or [],
        }
        try:
            response = self._session.post(
                url,
                json=payload,
                auth=self._auth,
                timeout=self._config.rpc_timeout,
                headers={"Content-Type": "application/json"},
            )
        except requests.Timeout as exc:
            raise RpcError(method, None, "request timed out", 504) from exc
        except requests.RequestException as exc:
            raise RpcError(method, None, "endpoint unreachable", 503) from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise RpcError(method, None, f"non-JSON response with HTTP {response.status_code}") from exc

        if response.status_code >= 400:
            error = body.get("error") if isinstance(body, dict) else None
            raise RpcError(
                method,
                error.get("code") if isinstance(error, dict) else None,
                _safe_rpc_message(error, response.status_code),
                502 if response.status_code < 500 else 503,
            )

        error = body.get("error")
        if error:
            raise RpcError(
                method,
                error.get("code") if isinstance(error, dict) else None,
                _safe_rpc_message(error, response.status_code),
            )
        return body.get("result")

    def _url(self, wallet: str | None) -> str:
        """Return the node URL or a path-escaped wallet-scoped RPC URL."""
        if not wallet:
            return self._config.rpc_url
        return f"{self._config.rpc_url}/wallet/{quote(wallet, safe='')}"


def _safe_rpc_message(error: Any, status_code: int) -> str:
    """Extract a bounded upstream error message without exposing arbitrary bodies."""
    if isinstance(error, dict):
        message = str(error.get("message") or "unknown error")
        return message[:500]
    return f"HTTP {status_code}"
