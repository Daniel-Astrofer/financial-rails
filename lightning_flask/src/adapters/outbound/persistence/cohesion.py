"""SQLite storage for idempotency, ownership leases, and prepared payments."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
import uuid

from src.application.security import ApiError


# Sensitive fields stripped from event metadata before storage.
# Preimage and route are stripped because they reveal payment path details
# that could be used to deanonymize the sender.
SENSITIVE_KEYS = {"macaroon", "paymentRequest", "preimage", "payment_preimage", "payment_route", "token", "secret"}

# Fields stripped from idempotent response cache (more restrictive than event metadata)
# payment_request is kept because it's needed for invoice replay
RESPONSE_SENSITIVE_KEYS = {"payment_preimage", "payment_route"}


@dataclass(frozen=True)
class IdempotencyClaim:
    """Fenced ownership token returned to one request worker.

    Attributes:
        key: Idempotency key scoped by the inbound API layer.
        token: Lease fencing token required for state transitions.
        state: Existing durable state at the time this worker acquired ownership.
        payment_hash: Known payment hash retained for retry reconciliation.
    """
    key: str
    token: str
    state: str
    payment_hash: str | None


@dataclass(frozen=True)
class IdempotencyReplay:
    """Previously completed HTTP result returned for a duplicate request.

    Attributes:
        payload: Cached response body after sensitive response fields were removed.
        status_code: HTTP status code associated with the original response.
    """
    payload: dict[str, Any]
    status_code: int


class CohesionStore:
    """Durable Lightning idempotency state and sanitized operational events.

    SQLite transactions serialize claim transitions across workers. Lease tokens
    fence stale owners, while event/response sanitizers keep payment secrets out
    of persisted operational records.
    """

    def __init__(self, path: str, claim_lease_seconds: int = 300):
        """Create private SQLite storage, enforce lease minimum, and initialize schema."""
        self.path = path
        self.claim_lease_seconds = max(5, int(claim_lease_seconds))
        self._ensure_path()
        self._init_db()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """Yield a durable SQLite connection with WAL and always close it."""
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA synchronous=FULL")
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Create idempotency/event tables, indexes, and additive claim migrations."""
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS idempotency (
                    key TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    claim_token TEXT,
                    claimed_at INTEGER,
                    lease_expires_at INTEGER,
                    state TEXT NOT NULL DEFAULT 'CLAIMED',
                    payment_hash TEXT,
                    payment_network TEXT NOT NULL DEFAULT '',
                    settled_at INTEGER,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS lightning_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    payment_hash TEXT,
                    amount_sats INTEGER,
                    status TEXT,
                    metadata_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    idempotency_key TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_lightning_events_created ON lightning_events(created_at);
                CREATE INDEX IF NOT EXISTS idx_lightning_events_payment_hash ON lightning_events(payment_hash);
                CREATE INDEX IF NOT EXISTS idx_idempotency_state ON idempotency(state);
                CREATE INDEX IF NOT EXISTS idx_idempotency_lease ON idempotency(lease_expires_at);
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(idempotency)").fetchall()}
            migrations = {
                "claim_token": "TEXT",
                "claimed_at": "INTEGER",
                "lease_expires_at": "INTEGER",
                "state": "TEXT NOT NULL DEFAULT 'CLAIMED'",
                "payment_hash": "TEXT",
                "payment_network": "TEXT NOT NULL DEFAULT ''",
                "settled_at": "INTEGER",
                "last_error": "TEXT",
            }
            for column, definition in migrations.items():
                if column not in columns:
                    conn.execute(f"ALTER TABLE idempotency ADD COLUMN {column} {definition}")
        self._restrict_permissions()

    def claim_idempotent(
        self, key: str, fingerprint: str, *, payment_hash: str | None = None, network: str = "",
    ) -> IdempotencyClaim | IdempotencyReplay:
        """Claim a request key, return its cached result, or reject conflicting work.

        A different fingerprint conflicts; a live lease blocks concurrent work;
        an expired lease may be acquired with a new token. Completed states are
        returned as replays and uncertain/submitted states retain payment identity.
        """
        now = int(time.time())
        claim_token = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT fingerprint, status_code, response_json, claim_token, state,
                           lease_expires_at, payment_hash
                    FROM idempotency WHERE key = ?
                    """,
                    (key,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO idempotency(
                            key, fingerprint, status_code, response_json, created_at,
                            claim_token, claimed_at, lease_expires_at, state,
                            payment_hash, payment_network
                        )
                        VALUES (?, ?, -1, '{}', ?, ?, ?, ?, 'CLAIMED', ?, ?)
                        """,
                        (
                            key,
                            fingerprint,
                            now,
                            claim_token,
                            now,
                            now + self.claim_lease_seconds,
                            payment_hash or "",
                            network,
                        ),
                    )
                    conn.execute("COMMIT")
                    return IdempotencyClaim(key, claim_token, "CLAIMED", payment_hash)

                if row["fingerprint"] != fingerprint:
                    raise ApiError(
                        "Idempotency-Key was reused with a different request",
                        409,
                        "idempotency_conflict",
                    )
                state = str(row["state"])
                if state in {"SUCCEEDED", "FAILED_FINAL"}:
                    conn.execute("COMMIT")
                    return IdempotencyReplay(
                        json.loads(row["response_json"]),
                        int(row["status_code"]),
                    )
                if row["claim_token"] and int(row["lease_expires_at"] or 0) > now:
                    raise ApiError(
                        "Request with this Idempotency-Key is still in progress",
                        409,
                        "idempotency_in_progress",
                    )
                updated = conn.execute(
                    """
                    UPDATE idempotency
                    SET claim_token = ?, claimed_at = ?, lease_expires_at = ?
                    WHERE key = ? AND fingerprint = ?
                      AND (claim_token IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= ?)
                    """,
                    (
                        claim_token,
                        now,
                        now + self.claim_lease_seconds,
                        key,
                        fingerprint,
                        now,
                    ),
                )
                if updated.rowcount != 1:
                    raise ApiError(
                        "Request with this Idempotency-Key is still in progress",
                        409,
                        "idempotency_in_progress",
                    )
                conn.execute("COMMIT")
                existing_hash = str(row["payment_hash"] or "") or payment_hash
                return IdempotencyClaim(key, claim_token, state, existing_hash)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def save_idempotent(
        self,
        key: str,
        fingerprint: str,
        claim_token: str,
        response: dict[str, Any],
        status_code: int,
    ) -> None:
        """Persist a sanitized completed response if the fencing token still matches."""
        safe_response = _strip_keys(response, RESPONSE_SENSITIVE_KEYS)
        with self._conn() as conn:
            cursor = conn.execute(
                """
                UPDATE idempotency
                SET status_code = ?, response_json = ?, state = 'SUCCEEDED', settled_at = ?,
                    claim_token = NULL, lease_expires_at = NULL, last_error = NULL
                WHERE key = ? AND fingerprint = ? AND claim_token = ?
                  AND state IN ('CLAIMED', 'SUBMITTED', 'UNKNOWN')
                """,
                (
                    status_code,
                    json.dumps(safe_response, separators=(",", ":")),
                    int(time.time()),
                    key,
                    fingerprint,
                    claim_token,
                ),
            )
            if cursor.rowcount != 1:
                raise self._lost_claim()

    def mark_submitted(self, key: str, fingerprint: str, claim_token: str, payment_hash: str) -> None:
        """Record LND submission and extend the lease while payment settles."""
        with self._conn() as conn:
            cursor = conn.execute(
                """
                UPDATE idempotency
                SET state = 'SUBMITTED', payment_hash = ?, lease_expires_at = ?
                WHERE key = ? AND fingerprint = ? AND claim_token = ? AND state = 'CLAIMED'
                """,
                (
                    payment_hash,
                    int(time.time()) + self.claim_lease_seconds,
                    key,
                    fingerprint,
                    claim_token,
                ),
            )
            if cursor.rowcount != 1:
                raise self._lost_claim()

    def mark_failed(
        self,
        key: str,
        fingerprint: str,
        claim_token: str,
        error_code: str,
        message: str,
        status_code: int = 400,
    ) -> None:
        """Persist a terminal payment failure and release the worker lease."""
        now = int(time.time())
        payload = json.dumps(
            {"success": False, "error": {"code": error_code, "message": message}},
            separators=(",", ":"),
        )
        with self._conn() as conn:
            cursor = conn.execute(
                """
                UPDATE idempotency
                SET status_code = ?, response_json = ?, state = 'FAILED_FINAL', settled_at = ?,
                    claim_token = NULL, lease_expires_at = NULL, last_error = ?
                WHERE key = ? AND fingerprint = ? AND claim_token = ?
                  AND state IN ('CLAIMED', 'SUBMITTED', 'UNKNOWN')
                """,
                (status_code, payload, now, message[:500], key, fingerprint, claim_token),
            )
            if cursor.rowcount != 1:
                raise self._lost_claim()

    def mark_unknown(
        self,
        key: str,
        fingerprint: str,
        claim_token: str,
        payment_hash: str,
        error: str,
    ) -> None:
        """Retain payment hash and lease when the upstream payment result is uncertain."""
        now = int(time.time())
        with self._conn() as conn:
            cursor = conn.execute(
                """
                UPDATE idempotency
                SET state = 'UNKNOWN', payment_hash = ?, lease_expires_at = ?, last_error = ?
                WHERE key = ? AND fingerprint = ? AND claim_token = ?
                  AND state IN ('SUBMITTED', 'UNKNOWN')
                """,
                (
                    payment_hash,
                    now + self.claim_lease_seconds,
                    (error or "Lightning payment result is unknown")[:500],
                    key,
                    fingerprint,
                    claim_token,
                ),
            )
            if cursor.rowcount != 1:
                raise self._lost_claim()

    def heartbeat_claim(self, key: str, fingerprint: str, claim_token: str) -> bool:
        """Extend an active idempotency lease if key, fingerprint, and token match."""
        now = int(time.time())
        with self._conn() as conn:
            cursor = conn.execute(
                """
                UPDATE idempotency SET lease_expires_at = ?
                WHERE key = ? AND fingerprint = ? AND claim_token = ?
                  AND state IN ('CLAIMED', 'SUBMITTED', 'UNKNOWN')
                """,
                (now + self.claim_lease_seconds, key, fingerprint, claim_token),
            )
            return cursor.rowcount > 0

    def query_payment_by_hash(self, payment_hash: str) -> dict[str, Any] | None:
        """Look up an idempotency record by payment hash for reconciliation."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT key, state, status_code, response_json, created_at FROM idempotency WHERE payment_hash=? ORDER BY created_at DESC",
                (payment_hash,),
            ).fetchone()
        if row is None:
            return None
        return {"key": row["key"], "state": row["state"], "status_code": row["status_code"],
                "response": json.loads(row["response_json"])}

    def state(self, key: str) -> dict[str, Any] | None:
        """Return reconciliation-relevant claim fields without cached response data."""
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT state, payment_hash, claim_token, lease_expires_at, last_error
                FROM idempotency WHERE key = ?
                """,
                (key,),
            ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _lost_claim() -> ApiError:
        """Build the standard HTTP conflict raised when claim fencing fails."""
        return ApiError(
            "This worker no longer owns the idempotency claim",
            409,
            "idempotency_claim_lost",
        )

    def _ensure_path(self) -> None:
        """Create the database path with owner-only permissions and reject symlinks."""
        path = Path(self.path).expanduser()
        if path.exists() and path.is_symlink():
            raise ValueError("LIGHTNING_BACKEND_SQLITE cannot be a symbolic link")
        parent = path.resolve(strict=False).parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.exists():
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        os.chmod(path, 0o600)

    def _restrict_permissions(self) -> None:
        """Restrict the database and SQLite sidecar files to owner read/write."""
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.path}{suffix}")
            if path.exists() and not path.is_symlink():
                os.chmod(path, 0o600)

    def append_event(
        self,
        event_type: str,
        *,
        payment_hash: str | None = None,
        amount_sats: int | None = None,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str = "",
    ) -> None:
        """Persist one operational event after removing configured secret fields."""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO lightning_events(event_type, payment_hash, amount_sats, status, metadata_json, created_at, idempotency_key)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    payment_hash,
                    amount_sats,
                    status,
                    json.dumps(_sanitize(metadata or {}), separators=(",", ":")),
                    int(time.time()),
                    idempotency_key,
                ),
            )

    def snapshot(self) -> dict[str, Any]:
        """Return table counts and up to 25 newest sanitized Lightning events."""
        with self._conn() as conn:
            idempotency_count = conn.execute("SELECT COUNT(*) AS count FROM idempotency").fetchone()["count"]
            event_count = conn.execute("SELECT COUNT(*) AS count FROM lightning_events").fetchone()["count"]
            unknown_count = conn.execute("SELECT COUNT(*) AS count FROM idempotency WHERE state='UNKNOWN'").fetchone()["count"]
            recent = conn.execute(
                """
                SELECT event_type, payment_hash, amount_sats, status, metadata_json, created_at, idempotency_key
                FROM lightning_events
                ORDER BY id DESC
                LIMIT 25
                """
            ).fetchall()
        return {
            "idempotency_records": idempotency_count,
            "unknown_idempotency_records": unknown_count,
            "lightning_events": event_count,
            "recent_events": [
                {
                    "event_type": row["event_type"],
                    "payment_hash": row["payment_hash"],
                    "amount_sats": row["amount_sats"],
                    "status": row["status"],
                    "metadata": json.loads(row["metadata_json"]),
                    "created_at": row["created_at"],
                    "idempotency_key": row["idempotency_key"],
                }
                for row in recent
            ],
        }


def _sanitize(metadata: dict[str, Any]) -> dict[str, Any]:
    """Remove top-level credential, preimage, and route keys from event metadata."""
    return {key: value for key, value in metadata.items() if key not in SENSITIVE_KEYS and key.lower() not in SENSITIVE_KEYS}


def _strip_keys(obj: Any, keys_to_strip: set[str]) -> Any:
    """Recursively strip specified keys from nested dicts/lists."""
    if isinstance(obj, dict):
        return {key: _strip_keys(value, keys_to_strip) for key, value in obj.items()
                if key not in keys_to_strip and key.lower() not in keys_to_strip}
    if isinstance(obj, list):
        return [_strip_keys(item, keys_to_strip) for item in obj]
    return obj
