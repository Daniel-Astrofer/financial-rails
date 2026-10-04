"""Input normalization and validation rules for Bitcoin wallet requests."""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any

from src.application.errors import ApiError

SATOSHIS_PER_BTC = Decimal("100000000")

_WALLET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{11,127}$")
_TXID_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_ADDRESS_RE = re.compile(r"^[A-Za-z0-9]{14,120}$")
_UNSIGNED_INTEGER_RE = re.compile(r"^[0-9]+$")


def validate_wallet_name(value: Any) -> str:
    """Validate a wallet name before it is interpolated into an RPC route.

    Names are limited to 1-64 URL-safe characters and reject path traversal.
    Raises ``ApiError`` with HTTP 400 when the value is unsafe or malformed.
    """
    if not isinstance(value, str) or not _WALLET_RE.fullmatch(value):
        raise ApiError(400, "INVALID_WALLET_NAME", "Wallet name must be 1-64 URL-safe characters.")
    if ".." in value or "/" in value or "\\" in value:
        raise ApiError(400, "INVALID_WALLET_NAME", "Wallet name must not contain path traversal.")
    return value


def validate_label(value: Any, default: str = "") -> str:
    """Return a printable label no longer than 128 characters.

    ``None`` selects ``default``; control characters and non-string values are
    rejected with an HTTP 400 ``INVALID_LABEL`` error.
    """
    if value is None:
        return default
    if not isinstance(value, str) or len(value) > 128 or any(ord(ch) < 32 for ch in value):
        raise ApiError(400, "INVALID_LABEL", "Label must be a printable string up to 128 characters.")
    return value


def validate_address_type(value: Any) -> str:
    """Normalize the requested Bitcoin address type and validate its allowlist.

    An omitted type defaults to ``bech32``. Unsupported values produce an API
    validation error instead of being passed to Bitcoin Core.
    """
    address_type = "bech32" if value is None else str(value)
    if address_type not in {"legacy", "p2sh-segwit", "bech32", "bech32m"}:
        raise ApiError(400, "INVALID_ADDRESS_TYPE", "Unsupported Bitcoin address type.")
    return address_type


def validate_address_literal(value: Any) -> str:
    """Apply the service's conservative lexical check to an address string.

    This checks printable alphanumeric shape only; network/version and
    checksum validation remain the responsibility of Bitcoin Core.
    """
    if not isinstance(value, str) or not _ADDRESS_RE.fullmatch(value):
        raise ApiError(400, "INVALID_BITCOIN_ADDRESS", "Bitcoin address format is invalid.")
    return value


def validate_txid(value: Any) -> str:
    """Validate a 64-character transaction hash and return lowercase hex."""
    if not isinstance(value, str) or not _TXID_RE.fullmatch(value):
        raise ApiError(400, "INVALID_TXID", "Transaction id must be a 64-character hex string.")
    return value.lower()


def validate_hex(value: Any, max_bytes: int = 400_000) -> str:
    """Validate an even-length hexadecimal payload against a decoded-byte limit.

    Returns lowercase hex. Malformed payloads yield HTTP 400; payloads over the
    byte limit yield HTTP 413.
    """
    if not isinstance(value, str) or len(value) % 2 != 0 or not _HEX_RE.fullmatch(value):
        raise ApiError(400, "INVALID_HEX", "Value must be even-length hex.")
    if len(value) // 2 > max_bytes:
        raise ApiError(413, "HEX_TOO_LARGE", "Raw transaction exceeds the configured size limit.")
    return value.lower()


def validate_idempotency_key(value: Any) -> str | None:
    """Validate an optional idempotency key without changing its value.

    Empty/missing values disable idempotency and return ``None``. Present keys
    must contain 12-128 allowed printable characters or an HTTP 400 is raised.
    """
    if value in {None, ""}:
        return None
    if not isinstance(value, str) or not _IDEMPOTENCY_RE.fullmatch(value):
        raise ApiError(
            400,
            "INVALID_IDEMPOTENCY_KEY",
            "Idempotency-Key must be 12-128 safe printable characters.",
        )
    return value


def parse_positive_int(value: Any, field: str, maximum: int | None = None) -> int:
    """Parse an integer strictly greater than zero and enforce an optional cap."""
    parsed = _parse_unsigned_int(value, field, "positive")
    if parsed <= 0:
        raise ApiError(400, "INVALID_INTEGER", f"{field} must be positive.")
    if maximum is not None and parsed > maximum:
        raise ApiError(400, "AMOUNT_EXCEEDS_LIMIT", f"{field} exceeds the configured limit.")
    return parsed


def parse_non_negative_int(value: Any, field: str, default: int = 0, maximum: int | None = None) -> int:
    """Parse a nonnegative integer, using ``default`` only when input is absent."""
    if value is None:
        parsed = default
    else:
        parsed = _parse_unsigned_int(value, field, "non-negative")
    if parsed < 0:
        raise ApiError(400, "INVALID_INTEGER", f"{field} must be non-negative.")
    if maximum is not None and parsed > maximum:
        raise ApiError(400, "INVALID_INTEGER", f"{field} exceeds the configured limit.")
    return parsed


def _parse_unsigned_int(value: Any, field: str, description: str) -> int:
    """Parse an integer or digit-only string while rejecting booleans and signs."""
    if isinstance(value, bool):
        raise ApiError(400, "INVALID_INTEGER", f"{field} must be a {description} integer.")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _UNSIGNED_INTEGER_RE.fullmatch(value):
        return int(value)
    raise ApiError(400, "INVALID_INTEGER", f"{field} must be a {description} integer.")


def sats_to_btc_string(sats: int) -> str:
    """Format satoshis as a fixed eight-decimal BTC string without rounding up."""
    amount = (Decimal(sats) / SATOSHIS_PER_BTC).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    return format(amount, "f")


def btc_to_sats(value: Any) -> int:
    """Convert a Bitcoin Core BTC value to whole satoshis using round-down semantics.

    Invalid decimal values are reported as HTTP 502 because they originate from
    the upstream node response rather than from a caller request.
    """
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ApiError(502, "INVALID_CORE_AMOUNT", "Bitcoin Core returned an invalid amount.") from exc
    return int((amount * SATOSHIS_PER_BTC).to_integral_value(rounding=ROUND_DOWN))


def normalize_outputs(body: dict[str, Any], max_outputs: int, max_send_sats: int) -> list[dict[str, Any]]:
    """Validate, merge duplicate-address outputs, and return a sorted spend list.

    Each output must contain a valid address and positive ``amountSats``. Both
    output count and merged/total value are capped before the RPC adapter sees
    the request. Duplicate addresses are combined to avoid ambiguous outputs.
    """
    raw_outputs = body.get("outputs")
    if not isinstance(raw_outputs, list) or not raw_outputs:
        raise ApiError(400, "INVALID_OUTPUTS", "outputs must be a non-empty list.")
    if len(raw_outputs) > max_outputs:
        raise ApiError(400, "TOO_MANY_OUTPUTS", "Transaction has too many outputs.")

    merged: dict[str, int] = {}
    for item in raw_outputs:
        if not isinstance(item, dict):
            raise ApiError(400, "INVALID_OUTPUT", "Each output must be an object.")
        address = validate_address_literal(item.get("address"))
        sats = parse_positive_int(item.get("amountSats"), "amountSats", max_send_sats)
        merged[address] = merged.get(address, 0) + sats
        if merged[address] > max_send_sats:
            raise ApiError(400, "AMOUNT_EXCEEDS_LIMIT", "Output amount exceeds the configured limit.")

    total = sum(merged.values())
    if total > max_send_sats:
        raise ApiError(400, "AMOUNT_EXCEEDS_LIMIT", "Transaction total exceeds the configured limit.")
    return [{"address": address, "amountSats": sats} for address, sats in sorted(merged.items())]


def request_fingerprint(method: str, path: str, body: Any) -> str:
    """Hash a canonical JSON representation for idempotency conflict detection.

    Method names are uppercased, object keys are sorted, and omitted bodies are
    represented as an empty object so semantically equivalent requests hash the
    same way regardless of input mapping order.
    """
    canonical = json.dumps(
        {"method": method.upper(), "path": path, "body": body if body is not None else {}},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
