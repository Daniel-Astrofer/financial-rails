"""Environment-backed configuration and validation for the Lightning service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class Settings:
    """Runtime configuration for the Lightning REST adapter.

    Attributes:
        api_token: Legacy token used when scoped tokens are not configured.
        read_token: Credential allowed to call read-only endpoints.
        write_token: Credential required for state-changing payment endpoints.
        admin_token: Credential for operator-only status endpoints.
        network: LND chain/network label expected by this service.
        lnd_rest_url: Base URL of LND's REST interface.
        lnd_macaroon_hex: Hex-encoded macaroon credential, if provided inline.
        lnd_macaroon_path: Filesystem path to the macaroon credential.
        lnd_tls_cert_path: Optional CA certificate used to verify LND TLS.
        lnd_timeout_seconds: Maximum duration for an LND REST request.
        sqlite_path: Persistent local path for idempotency/cohesion state.
        max_body_bytes: Maximum size of an inbound HTTP request body.
        rate_limit_per_minute: Request allowance per key and minute.
        rate_limit_backend: In-process or shared Redis limiter implementation.
        redis_url: Redis URL when shared rate limiting is enabled.
        status_cache_seconds: Cache duration for repeated node status requests.
        max_invoice_sats: Maximum amount accepted when creating an invoice.
        max_payment_sats: Maximum amount accepted when paying an invoice.
        max_fee_sats: Absolute fee ceiling for an outgoing payment.
        max_fee_ppm: Proportional fee ceiling in parts per million.
        max_daily_payment_sats: Aggregate outgoing value limit per day.
        max_in_flight_sats: Aggregate unsettled payment exposure limit.
        default_invoice_expiry_seconds: Expiry used when a caller omits one.
        auth_disabled: Development-only switch that bypasses bearer auth.
        production: Whether strict production gates must apply.
        tls_enabled: Whether inbound HTTPS is enabled at the service boundary.
        allow_insecure_lnd: Explicit private-network exception for HTTP to LND.
        instance_count: Replica count sharing the SQLite state store.
    """
    api_token: str
    read_token: str
    write_token: str
    admin_token: str
    network: str
    lnd_rest_url: str
    lnd_macaroon_hex: str = ""
    lnd_macaroon_path: str = ""
    lnd_tls_cert_path: str = ""
    lnd_timeout_seconds: float = 8.0
    sqlite_path: str = "/var/lib/kerosene/lightning-backend/state.sqlite3"
    max_body_bytes: int = 64 * 1024
    rate_limit_per_minute: int = 120
    rate_limit_backend: str = "memory"
    redis_url: str = ""
    status_cache_seconds: float = 2.0
    max_invoice_sats: int = 50_000_000
    max_payment_sats: int = 50_000_000
    max_fee_sats: int = 5_000
    max_fee_ppm: int = 500
    max_daily_payment_sats: int = 100_000_000
    max_in_flight_sats: int = 50_000_000
    default_invoice_expiry_seconds: int = 3600
    auth_disabled: bool = False
    production: bool = False
    tls_enabled: bool = False
    allow_insecure_lnd: bool = False
    instance_count: int = 1

    @classmethod
    def from_env(cls) -> "Settings":
        """Load Lightning settings and apply legacy token compatibility fallback."""
        read_token = os.getenv("LIGHTNING_READ_TOKEN", "")
        write_token = os.getenv("LIGHTNING_WRITE_TOKEN", "")
        api_token = os.getenv("KEROSENE_API_TOKEN", "")
        auth_disabled = os.getenv("LIGHTNING_AUTH_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}
        production = os.getenv("LIGHTNING_PRODUCTION", "").strip().lower() in {"1", "true", "yes", "on"}
        if api_token and not read_token and not write_token:
            read_token = api_token
            write_token = api_token
        return cls(
            api_token=api_token,
            read_token=read_token,
            write_token=write_token,
            admin_token=os.getenv("LIGHTNING_ADMIN_TOKEN", ""),
            network=os.getenv("LIGHTNING_NETWORK", "mainnet").lower(),
            lnd_rest_url=os.getenv("LIGHTNING_LND_REST_URL", "https://127.0.0.1:8080"),
            lnd_macaroon_hex=os.getenv("LIGHTNING_LND_MACAROON_HEX", ""),
            lnd_macaroon_path=os.getenv("LIGHTNING_LND_MACAROON_PATH", ""),
            lnd_tls_cert_path=os.getenv("LIGHTNING_LND_TLS_CERT_PATH", ""),
            lnd_timeout_seconds=float(os.getenv("LIGHTNING_LND_TIMEOUT_SECONDS", "8")),
            sqlite_path=os.getenv(
                "LIGHTNING_BACKEND_SQLITE",
                "/var/lib/kerosene/lightning-backend/state.sqlite3",
            ),
            max_body_bytes=int(os.getenv("LIGHTNING_BACKEND_MAX_BODY_BYTES", str(64 * 1024))),
            rate_limit_per_minute=int(os.getenv("LIGHTNING_BACKEND_RATE_LIMIT_PER_MINUTE", "120")),
            rate_limit_backend=os.getenv("LIGHTNING_RATE_LIMIT_BACKEND", "memory").lower(),
            redis_url=os.getenv("LIGHTNING_REDIS_URL", ""),
            status_cache_seconds=float(os.getenv("LIGHTNING_BACKEND_STATUS_CACHE_SECONDS", "2")),
            max_invoice_sats=int(os.getenv("LIGHTNING_BACKEND_MAX_INVOICE_SATS", "50000000")),
            max_payment_sats=int(os.getenv("LIGHTNING_BACKEND_MAX_PAYMENT_SATS", "50000000")),
            max_fee_sats=int(os.getenv("LIGHTNING_BACKEND_MAX_FEE_SATS", "5000")),
            max_fee_ppm=int(os.getenv("LIGHTNING_BACKEND_MAX_FEE_PPM", "500")),
            max_daily_payment_sats=int(os.getenv("LIGHTNING_BACKEND_MAX_DAILY_PAYMENT_SATS", "100000000")),
            max_in_flight_sats=int(os.getenv("LIGHTNING_BACKEND_MAX_IN_FLIGHT_SATS", "50000000")),
            default_invoice_expiry_seconds=int(os.getenv("LIGHTNING_DEFAULT_INVOICE_EXPIRY_SECONDS", "3600")),
            auth_disabled=auth_disabled,
            production=production,
            tls_enabled=_bool_env("LIGHTNING_TLS_ENABLED"),
            allow_insecure_lnd=_bool_env("LIGHTNING_LND_ALLOW_INSECURE"),
            instance_count=_int_env("LIGHTNING_BACKEND_INSTANCE_COUNT", 1, 1),
        )

    def validate(self, bind_host: str | None = None) -> None:
        """Enforce token, network, transport, limit, and persistence invariants.

        A non-loopback bind is treated as production exposure and requires TLS
        and authentication even when the explicit production flag is false.
        """
        host = (bind_host or os.getenv("HOST", "127.0.0.1")).strip()
        # When production=true and HOST is default/loopback, assume externally bound.
        # The real bind is set independently by the WSGI launcher.
        if self.production and (not host or host == "127.0.0.1" or host == "localhost"):
            host = "0.0.0.0"
        externally_bound = not _is_loopback_host(host)
        production = self.production or externally_bound
        if self.auth_disabled and production:
            raise ValueError("LIGHTNING_AUTH_DISABLED cannot be true in production")
        if externally_bound and not self.tls_enabled:
            raise ValueError("LIGHTNING_TLS_ENABLED=true is required on a non-loopback bind")

        # Validate token entropy (minimum 32 bytes = 64 hex or ~43 base64 characters).
        def _valid_token(tok: str) -> bool:
            """Accept only hex or base64-like tokens with at least 32 bytes entropy."""
            if not tok or len(tok) < 43:
                return False
            import re
            if re.fullmatch(r'[0-9a-fA-F]{64,}', tok):
                # hex: 32+ bytes
                return True
            if re.fullmatch(r'[A-Za-z0-9+/=_-]{43,}', tok):
                # base64url: ~32+ bytes
                return True
            return False

        for name, tok in [("LIGHTNING_READ_TOKEN", self.read_token),
                          ("LIGHTNING_WRITE_TOKEN", self.write_token),
                          ("LIGHTNING_ADMIN_TOKEN", self.admin_token)]:
            if tok and not _valid_token(tok):
                raise ValueError(f"{name} must have at least 32 bytes of entropy (64 hex or ~43 base64 chars)")
        if not self.read_token and not self.write_token:
            if not _valid_token(self.api_token):
                raise ValueError("KEROSENE_API_TOKEN must have at least 32 bytes of entropy (64 hex or ~43 base64 chars)")

        if self.network not in {"mainnet", "testnet", "regtest", "signet"}:
            raise ValueError("LIGHTNING_NETWORK must be mainnet, testnet, regtest, or signet")
        if not self.lnd_rest_url.startswith(("http://", "https://")):
            raise ValueError("LIGHTNING_LND_REST_URL must use http or https")
        if not self.lnd_macaroon_hex and not self.lnd_macaroon_path:
            raise ValueError("LIGHTNING_LND_MACAROON_HEX or LIGHTNING_LND_MACAROON_PATH is required")
        if self.max_invoice_sats < 1 or self.max_payment_sats < 1:
            raise ValueError("Lightning amount limits must be positive")
        if self.max_fee_sats < 1:
            raise ValueError("LIGHTNING_BACKEND_MAX_FEE_SATS must be positive")
        if self.max_fee_ppm < 1:
            raise ValueError("LIGHTNING_BACKEND_MAX_FEE_PPM must be positive")
        if self.rate_limit_backend not in {"memory", "redis"}:
            raise ValueError("LIGHTNING_RATE_LIMIT_BACKEND must be 'memory' or 'redis'")
        if self.rate_limit_backend == "redis" and not self.redis_url:
            raise ValueError("LIGHTNING_REDIS_URL is required when LIGHTNING_RATE_LIMIT_BACKEND=redis")
        if production:
            if not self.read_token or not self.write_token:
                raise ValueError("LIGHTNING_READ_TOKEN and LIGHTNING_WRITE_TOKEN are required in production")
            self._validate_lnd_transport()
            self._validate_state_store()

    def _validate_lnd_transport(self) -> None:
        """Require HTTPS, allowing HTTP only for explicitly trusted private hosts."""
        parsed = urlparse(self.lnd_rest_url)
        if parsed.scheme == "https":
            return
        if parsed.scheme != "http" or not self.allow_insecure_lnd:
            raise ValueError(
                "LIGHTNING_LND_REST_URL must use HTTPS in production; "
                "LIGHTNING_LND_ALLOW_INSECURE=true is restricted to isolated private networks"
            )
        if not _is_private_host(parsed.hostname or ""):
            raise ValueError("Insecure LND transport is only permitted for private service hosts")

    def _validate_state_store(self) -> None:
        """Require durable absolute SQLite storage and a single service replica."""
        path = Path(self.sqlite_path).expanduser()
        if not path.is_absolute():
            raise ValueError("LIGHTNING_BACKEND_SQLITE must be an absolute persistent path in production")
        resolved = path.resolve(strict=False)
        ephemeral_roots = (Path("/tmp"), Path("/var/tmp"), Path("/dev/shm"))
        if any(resolved == root or root in resolved.parents for root in ephemeral_roots):
            raise ValueError("LIGHTNING_BACKEND_SQLITE cannot use ephemeral storage in production")
        if self.instance_count != 1:
            raise ValueError(
                "The SQLite idempotency store requires LIGHTNING_BACKEND_INSTANCE_COUNT=1; "
                "run a single replica with a persistent volume"
            )


def _bool_env(name: str) -> bool:
    """Parse common affirmative environment values as a boolean."""
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int, minimum: int) -> int:
    """Parse an integer environment value and reject values below its minimum."""
    value = int(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _is_loopback_host(host: str) -> bool:
    """Recognize loopback DNS names and IP addresses."""
    normalized = host.strip().strip("[]").lower()
    if normalized in {"localhost", "ip6-localhost"}:
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def _is_private_host(host: str) -> bool:
    """Recognize loopback, private IP, and cluster-local service hostnames."""
    normalized = host.strip().strip("[]").lower()
    if _is_loopback_host(normalized):
        return True
    try:
        return ip_address(normalized).is_private
    except ValueError:
        return "." not in normalized or normalized.endswith((".internal", ".local", ".svc", ".svc.cluster.local"))
