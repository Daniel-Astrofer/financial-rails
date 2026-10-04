"""Authentication, request validation, and sensitive-field redaction helpers."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
from collections import defaultdict, deque
from typing import Any

from flask import Request

logger = logging.getLogger(__name__)

INVOICE_RE = re.compile(r"^(ln(bc|tb|bcrt|bs)[0-9a-z]{20,4096})$", re.IGNORECASE)
HEX_32_RE = re.compile(r"^[0-9a-fA-F]{64}$")
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{7,127}$")
# Payment operations use a stricter, namespaced idempotency-key policy.
IDEMPOTENCY_KEY_PAYMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{15,127}$")

class ApiError(Exception):
    """Expected HTTP failure carrying response-safe text, status, and error code.

    Attributes:
        message: Safe human-readable description returned to the API caller.
        status_code: HTTP status selected by the application policy.
        code: Stable machine-readable failure category.
    """

    def __init__(self, message: str, status_code: int = 400, code: str = "bad_request"):
        """Initialize the exception and retain fields used by the Flask handler."""
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


def request_fingerprint(request: Request) -> str:
    """Hash method, path, and exact cached request bytes for idempotency checks."""
    body = request.get_data(cache=True) or b""
    return hashlib.sha256(request.method.encode() + b"\n" + request.path.encode() + b"\n" + body).hexdigest()


def require_json_object(value: Any) -> dict[str, Any]:
    """Require a decoded request body to be a JSON object."""
    if not isinstance(value, dict):
        raise ApiError("JSON body must be an object", 400, "invalid_json")
    return value


def parse_sats(value: Any, field: str, maximum: int) -> int:
    """Parse a positive integer satoshi amount and enforce the configured ceiling."""
    if isinstance(value, bool):
        raise ApiError(f"{field} must be an integer number of sats", 400, "invalid_amount")
    try:
        sats = int(str(value))
    except (TypeError, ValueError):
        raise ApiError(f"{field} must be an integer number of sats", 400, "invalid_amount") from None
    if sats <= 0:
        raise ApiError(f"{field} must be positive", 400, "invalid_amount")
    if sats > maximum:
        raise ApiError(f"{field} exceeds configured limit", 400, "amount_too_large")
    return sats


def parse_optional_int(value: Any, field: str, default: int, minimum: int, maximum: int) -> int:
    """Parse an optional bounded integer, applying the default only to `None`."""
    if value is None:
        return default
    if isinstance(value, bool):
        raise ApiError(f"{field} must be an integer", 400, "invalid_integer")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        raise ApiError(f"{field} must be an integer", 400, "invalid_integer") from None
    if parsed < minimum or parsed > maximum:
        raise ApiError(f"{field} is outside allowed range", 400, "invalid_integer")
    return parsed


def validate_memo(value: Any) -> str:
    """Convert the invoice memo to text and reject values longer than 256 chars."""
    memo = str(value or "")
    if len(memo) > 256:
        raise ApiError("memo is too long", 400, "invalid_memo")
    return memo


def validate_bolt11(value: Any) -> str:
    """Trim and validate a BOLT11 invoice string for supported network prefixes."""
    invoice = str(value or "").strip()
    if not INVOICE_RE.fullmatch(invoice):
        raise ApiError("payment_request must be a BOLT11 Lightning invoice", 400, "invalid_invoice")
    return invoice


def validate_payment_hash(value: Any) -> str:
    """Validate a 32-byte payment hash and normalize it to lowercase hex."""
    payment_hash = str(value or "").strip()
    if not HEX_32_RE.fullmatch(payment_hash):
        raise ApiError("payment_hash must be 32 bytes encoded as hex", 400, "invalid_payment_hash")
    return payment_hash.lower()


def validate_idempotency_key(value: Any) -> str | None:
    """Validate a general optional key; return `None` when no key was supplied."""
    if value in {None, ""}:
        return None
    key = str(value).strip()
    if not IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise ApiError(
            "Idempotency-Key must be at least 8 safe printable characters",
            400,
            "invalid_idempotency_key",
        )
    return key


def validate_idempotency_key_payment(value: Any) -> str | None:
    """Validate the stricter idempotency-key policy for payment operations.
    Requires 16-128 chars, alphanumeric + safe symbols.
    Returns None if empty, raises on invalid."""
    if value in {None, ""}:
        return None
    key = str(value).strip()
    if not IDEMPOTENCY_KEY_PAYMENT_RE.fullmatch(key):
        raise ApiError(
            "Idempotency-Key must be 16-128 safe printable characters for payment operations",
            400,
            "invalid_idempotency_key",
        )
    return key


def require_bearer_token_scoped(
    request: Request,
    read_token: str,
    write_token: str,
    require_write: bool = False,
) -> None:
    """Authenticate with scoped bearer tokens.
    GET/HEAD/OPTIONS: accept READ or WRITE token
    POST/PUT/PATCH/DELETE: require WRITE token
    If only one token configured, use it for both scopes."""
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        raise ApiError("Missing bearer token", 401, "unauthorized")
    supplied = header[len(prefix):].strip()

    if write_token and hmac.compare_digest(supplied, write_token):
        return
    if read_token and hmac.compare_digest(supplied, read_token) and not require_write:
        return
    if not read_token and not write_token:
        raise ApiError("No auth tokens configured", 500, "configuration_error")
    raise ApiError("Invalid bearer token or insufficient scope", 403, "forbidden")


def require_admin_header(request: Request, admin_token: str) -> None:
    """Check X-Kerosene-Admin-Key header for admin endpoints.
    If no admin token configured, raise 404 to hide existence."""
    if not admin_token:
        raise ApiError("Not found", 404, "not_found")
    supplied = request.headers.get("X-Kerosene-Admin-Key", "").strip()
    if not supplied or not hmac.compare_digest(supplied, admin_token):
        raise ApiError("Not found", 404, "not_found")


class RateLimiter:
    """Single-process sliding-window request limiter keyed by authenticated principal."""

    def __init__(self, limit_per_minute: int):
        """Set a per-minute allowance, clamped to at least one request."""
        self.limit = max(1, int(limit_per_minute))
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        """Record the hit or raise HTTP 429 after pruning expired timestamps."""
        now = time.monotonic()
        bucket = self._hits[key]
        cutoff = now - 60
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self.limit:
            raise ApiError("Too many requests", 429, "rate_limited")
        bucket.append(now)


class RedisRateLimiter:
    """Redis-backed distributed rate limiter for multi-worker deployments.

    When Redis is unavailable, ``fail_open`` controls whether requests continue
    or receive HTTP 503; requests exceeding the counter still receive HTTP 429.
    """

    def __init__(self, redis_url: str, limit_per_minute: int, *, fail_open: bool = True):
        """Configure the Redis connection, allowance, and outage behavior."""
        self._redis_url = redis_url
        self.limit = max(1, int(limit_per_minute))
        self._fail_open = fail_open
        self._redis: Any = None

    def _ensure_redis(self) -> Any:
        """Lazily connect/ping Redis and cache the client or an unavailable sentinel."""
        if self._redis is not None:
            return self._redis
        try:
            import redis as redis_lib
            self._redis = redis_lib.Redis.from_url(self._redis_url, socket_timeout=5)
            self._redis.ping()
        except Exception as exc:
            logger.error("Redis rate limiter unavailable: %s — falling back to in-memory", exc)
            self._redis = False
        return self._redis

    def check(self, key: str) -> None:
        """Increment a per-key minute counter and enforce limit/fail-open behavior."""
        r = self._ensure_redis()
        if r is False:
            if self._fail_open:
                logger.warning("Redis rate limiting unavailable; allowing request in development mode")
                return
            raise ApiError("Rate limit service is unavailable", 503, "rate_limit_unavailable")
        try:
            redis_key = f"ratelimit:{key}:60"
            count = r.incr(redis_key)
            if count == 1:
                r.expire(redis_key, 60)
            if count > self.limit:
                raise ApiError("Too many requests", 429, "rate_limited")
        except ApiError:
            raise
        except Exception as exc:
            logger.warning("Redis rate limit check failed: %s", exc)
            if self._fail_open:
                return
            raise ApiError("Rate limit service is unavailable", 503, "rate_limit_unavailable") from exc
