"""Environment-backed settings and validation for the Bitcoin Core service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import FrozenSet
from urllib.parse import urlparse


def _bool_env(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable using common affirmative spellings."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int, minimum: int | None = None) -> int:
    """Parse an integer environment variable and enforce an optional minimum."""
    value = os.getenv(name)
    if value is None or value == "":
        parsed = default
    else:
        parsed = int(value)
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return parsed


def _float_env(name: str, default: float, minimum: float | None = None) -> float:
    """Parse a floating-point environment variable and enforce an optional minimum."""
    value = os.getenv(name)
    if value is None or value == "":
        parsed = default
    else:
        parsed = float(value)
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return parsed


def _api_keys() -> FrozenSet[str]:
    """Read legacy/general API keys and reject tokens below the entropy threshold."""
    raw = os.getenv("BITCOIN_BACKEND_API_KEYS") or os.getenv("KEROSENE_BACKEND_API_KEYS") or ""
    keys = frozenset(item.strip() for item in raw.split(",") if item.strip())
    weak = [key for key in keys if _token_entropy(key) < 32]
    if weak:
        raise ValueError("BITCOIN_BACKEND_API_KEYS entries must have at least 32 bytes of entropy (64 hex or ~43 base64 chars)")
    return keys


def _read_api_keys() -> FrozenSet[str]:
    """Read keys authorized for read-only routes and validate token strength."""
    raw = os.getenv("BITCOIN_BACKEND_READ_API_KEYS") or ""
    keys = frozenset(item.strip() for item in raw.split(",") if item.strip())
    weak = [key for key in keys if _token_entropy(key) < 32]
    if weak:
        raise ValueError("BITCOIN_BACKEND_READ_API_KEYS entries must have at least 32 bytes of entropy (64 hex or ~43 base64 chars)")
    return keys


def _write_api_keys() -> FrozenSet[str]:
    """Read keys authorized for mutating routes and validate token strength."""
    raw = os.getenv("BITCOIN_BACKEND_WRITE_API_KEYS") or ""
    keys = frozenset(item.strip() for item in raw.split(",") if item.strip())
    weak = [key for key in keys if _token_entropy(key) < 32]
    if weak:
        raise ValueError("BITCOIN_BACKEND_WRITE_API_KEYS entries must have at least 32 bytes of entropy (64 hex or ~43 base64 chars)")
    return keys


def _token_entropy(token: str) -> int:
    """Estimate entropy in bytes for a token.
    Accepts hex (64+ chars) or base64url (~43+ chars)."""
    import re
    if re.fullmatch(r'[0-9a-fA-F]{64,}', token):
        return len(token) // 2
    if re.fullmatch(r'[A-Za-z0-9+/=_-]{43,}', token):
        raw = len(token.rstrip("="))
        return raw * 6 // 8
    return 0


@dataclass(frozen=True)
class AppConfig:
    """Validated runtime settings for the Bitcoin Core adapter service.

    Attributes:
        rpc_url: Bitcoin Core JSON-RPC base URL.
        rpc_user: RPC Basic authentication username.
        rpc_password: RPC Basic authentication password.
        default_wallet: Wallet name used for wallet-scoped RPC calls.
        chain: Configured Bitcoin network name.
        api_keys: Backward-compatible key set used when scoped sets are absent.
        read_api_keys: Credentials accepted for read-only endpoints.
        write_api_keys: Credentials accepted for state-changing endpoints.
        admin_token: Credential for administrative endpoints.
        auth_disabled: Local-development switch that bypasses API-key checks.
        allow_wallet_create: Whether the adapter may create a wallet through RPC.
        allow_broadcast: Whether the adapter may broadcast transactions.
        connect_timeout_seconds: Connection timeout for Bitcoin Core RPC.
        read_timeout_seconds: Response timeout for Bitcoin Core RPC.
        rpc_pool_size: Maximum concurrent connections in the RPC pool.
        max_content_length: Maximum inbound HTTP body size in bytes.
        max_outputs_per_tx: Maximum number of distinct transaction outputs.
        max_send_sats: Maximum aggregate outgoing value, in satoshis.
        default_min_confirmations: Confirmation threshold for balance queries.
        idempotency_ttl_seconds: Lifetime of idempotency records.
        state_db_path: SQLite path for idempotency and cohesion state.
        rate_limit_per_minute: Request allowance per key and time window.
        rate_limit_backend: `memory` or shared `redis` implementation.
        redis_url: Redis connection URL when Redis limiting is selected.
        production: Whether production transport/authentication gates apply.
        tls_enabled: Whether inbound HTTPS termination is enabled.
        allow_insecure_rpc: Explicit exception for private-network HTTP RPC.
        instance_count: Number of replicas sharing the SQLite state store.
    """
    rpc_url: str
    rpc_user: str
    rpc_password: str
    default_wallet: str
    chain: str
    api_keys: FrozenSet[str]
    read_api_keys: FrozenSet[str]
    write_api_keys: FrozenSet[str]
    admin_token: str
    auth_disabled: bool
    allow_wallet_create: bool
    allow_broadcast: bool
    connect_timeout_seconds: float
    read_timeout_seconds: float
    rpc_pool_size: int
    max_content_length: int
    max_outputs_per_tx: int
    max_send_sats: int
    default_min_confirmations: int
    idempotency_ttl_seconds: int
    state_db_path: str
    rate_limit_per_minute: int
    rate_limit_backend: str
    redis_url: str
    production: bool = False
    tls_enabled: bool = False
    allow_insecure_rpc: bool = False
    instance_count: int = 1

    @classmethod
    def from_env(cls) -> "AppConfig":
        """Build configuration from environment variables and apply auth fallback rules.

        Scoped read/write keys take precedence. If neither is configured, the
        legacy key set serves both scopes; without keys, authentication may only
        be disabled explicitly for local development.
        """
        auth_disabled = _bool_env("BITCOIN_BACKEND_AUTH_DISABLED", False)
        keys = _api_keys()
        read_keys = _read_api_keys()
        write_keys = _write_api_keys()
        admin_token = os.getenv("BITCOIN_BACKEND_ADMIN_TOKEN", "")
        # Backward compat: if scoped keys are empty, fall back to api_keys for both
        if not read_keys and not write_keys:
            if keys:
                read_keys = keys
                write_keys = keys
            elif not auth_disabled:
                raise ValueError(
                    "Set BITCOIN_BACKEND_API_KEYS, BITCOIN_BACKEND_READ_API_KEYS / "
                    "BITCOIN_BACKEND_WRITE_API_KEYS, or explicitly set "
                    "BITCOIN_BACKEND_AUTH_DISABLED=true for local development."
                )

        return cls(
            rpc_url=os.getenv("BITCOIN_RPC_URL", "http://bitcoin-core:8332").rstrip("/"),
            rpc_user=os.getenv("BITCOIN_RPC_USER", ""),
            rpc_password=os.getenv("BITCOIN_RPC_PASSWORD", ""),
            default_wallet=os.getenv("BITCOIN_RPC_WALLET", "kerosene"),
            chain=os.getenv("BITCOIN_CHAIN", "mainnet"),
            api_keys=keys,
            read_api_keys=read_keys,
            write_api_keys=write_keys,
            admin_token=admin_token,
            auth_disabled=auth_disabled,
            allow_wallet_create=_bool_env("BITCOIN_BACKEND_ALLOW_WALLET_CREATE", False),
            allow_broadcast=_bool_env("BITCOIN_BACKEND_ALLOW_BROADCAST", False),
            connect_timeout_seconds=_float_env("BITCOIN_BACKEND_CONNECT_TIMEOUT_SECONDS", 2.0, 0.1),
            read_timeout_seconds=_float_env("BITCOIN_BACKEND_READ_TIMEOUT_SECONDS", 20.0, 0.1),
            rpc_pool_size=_int_env("BITCOIN_BACKEND_RPC_POOL_SIZE", 16, 1),
            max_content_length=_int_env("BITCOIN_BACKEND_MAX_CONTENT_LENGTH", 64 * 1024, 1024),
            max_outputs_per_tx=_int_env("BITCOIN_BACKEND_MAX_OUTPUTS_PER_TX", 64, 1),
            max_send_sats=_int_env("BITCOIN_BACKEND_MAX_SEND_SATS", 10_000_000, 1),
            default_min_confirmations=_int_env("BITCOIN_BACKEND_MIN_CONFIRMATIONS", 1, 0),
            idempotency_ttl_seconds=_int_env("BITCOIN_BACKEND_IDEMPOTENCY_TTL_SECONDS", 24 * 60 * 60, 60),
            state_db_path=os.getenv(
                "BITCOIN_BACKEND_DB_PATH",
                "/var/lib/kerosene/bitcoin-core-backend/state.sqlite3",
            ),
            rate_limit_per_minute=_int_env("BITCOIN_BACKEND_RATE_LIMIT_PER_MINUTE", 120, 1),
            rate_limit_backend=os.getenv("BITCOIN_BACKEND_RATE_LIMIT_BACKEND", "memory").lower(),
            redis_url=os.getenv("BITCOIN_BACKEND_REDIS_URL", ""),
            production=_bool_env("BITCOIN_BACKEND_PRODUCTION", False),
            tls_enabled=_bool_env("BITCOIN_BACKEND_TLS_ENABLED", False),
            allow_insecure_rpc=_bool_env("BITCOIN_RPC_ALLOW_INSECURE", False),
            instance_count=_int_env("BITCOIN_BACKEND_INSTANCE_COUNT", 1, 1),
        )

    @property
    def rpc_timeout(self) -> tuple[float, float]:
        """Return connect/read timeout values in the order expected by requests."""
        return (self.connect_timeout_seconds, self.read_timeout_seconds)

    def validate(self, bind_host: str) -> None:
        """Enforce deployment invariants based on production and bind exposure.

        Production requires scoped strong keys, RPC credentials, safe RPC
        transport, and persistent single-instance SQLite storage. Any externally
        bound address also requires inbound TLS and authentication.
        """
        host = (bind_host or "").strip().lower()
        # When host is unset but production mode is true, assume externally bound.
        # The real bind is set independently by the WSGI launcher (gunicorn --bind).
        # Failing closed: if production=true and HOST is default/loopback, enforce TLS anyway.
        if self.production and (not host or host == "127.0.0.1" or host == "localhost"):
            host = "0.0.0.0"
        externally_bound = not _is_loopback_host(host)
        production = self.production or externally_bound

        if self.rate_limit_backend not in {"memory", "redis"}:
            raise ValueError("BITCOIN_BACKEND_RATE_LIMIT_BACKEND must be 'memory' or 'redis'")
        if self.rate_limit_backend == "redis" and not self.redis_url:
            raise ValueError(
                "BITCOIN_BACKEND_REDIS_URL is required when BITCOIN_BACKEND_RATE_LIMIT_BACKEND=redis"
            )
        if self.auth_disabled and production:
            raise ValueError("Bitcoin backend authentication cannot be disabled in production")
        if externally_bound and not self.tls_enabled:
            raise ValueError(
                "BITCOIN_BACKEND_TLS_ENABLED=true is required when binding to a non-loopback address"
            )
        if not production:
            return

        if not self.read_api_keys or not self.write_api_keys:
            raise ValueError("Scoped read and write API keys are required in production")
        if not self.rpc_user.strip() or not self.rpc_password:
            raise ValueError("BITCOIN_RPC_USER and BITCOIN_RPC_PASSWORD are required in production")
        self._validate_rpc_transport()
        self._validate_state_store()

    def _validate_rpc_transport(self) -> None:
        """Require HTTPS RPC, unless private-network HTTP is explicitly allowed."""
        parsed = urlparse(self.rpc_url)
        if parsed.scheme == "https":
            return
        if parsed.scheme != "http":
            raise ValueError("BITCOIN_RPC_URL must use https in production")
        if not self.allow_insecure_rpc:
            raise ValueError(
                "Plain HTTP Bitcoin RPC is disabled in production; terminate TLS/mTLS at the RPC boundary "
                "or explicitly set BITCOIN_RPC_ALLOW_INSECURE=true for an isolated private network"
            )
        if not _is_private_rpc_host(parsed.hostname or ""):
            raise ValueError(
                "BITCOIN_RPC_ALLOW_INSECURE=true is only permitted for loopback, RFC1918, or private service hosts"
            )

    def _validate_state_store(self) -> None:
        """Require an absolute durable SQLite path and exactly one service replica."""
        path = Path(self.state_db_path).expanduser()
        if not path.is_absolute():
            raise ValueError("BITCOIN_BACKEND_DB_PATH must be an absolute persistent path in production")
        resolved = path.resolve(strict=False)
        ephemeral_roots = (Path("/tmp"), Path("/var/tmp"), Path("/dev/shm"))
        if any(resolved == root or root in resolved.parents for root in ephemeral_roots):
            raise ValueError("BITCOIN_BACKEND_DB_PATH cannot use ephemeral storage in production")
        if self.instance_count != 1:
            raise ValueError(
                "The SQLite idempotency store requires BITCOIN_BACKEND_INSTANCE_COUNT=1; "
                "run a single replica with a persistent volume"
            )


def _is_loopback_host(host: str) -> bool:
    """Recognize localhost names and IP addresses that route to loopback."""
    normalized = host.strip().strip("[]").lower()
    if normalized in {"localhost", "ip6-localhost"}:
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def _is_private_rpc_host(host: str) -> bool:
    """Allow only loopback, private IPs, or unqualified/private-service DNS names."""
    normalized = host.strip().strip("[]").lower()
    if _is_loopback_host(normalized):
        return True
    try:
        address = ip_address(normalized)
        return address.is_private
    except ValueError:
        # Unqualified names and cluster-local DNS names are explicit private service endpoints.
        return "." not in normalized or normalized.endswith((".internal", ".local", ".svc", ".svc.cluster.local"))
