"""SQLite-backed idempotency, ownership fencing, and broadcast recovery store."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
import uuid

from src.application.errors import ApiError


@dataclass(frozen=True)
class IdempotencyReplay:
    """Cached API result returned when a completed idempotent request repeats.

    Attributes:
        status_code: HTTP status persisted for the original request.
        response: Decoded JSON response body, copied for the replay response.
    """
    status_code: int
    response: dict[str, Any]


@dataclass(frozen=True)
class IdempotencyClaim:
    """Ownership token and durable state returned after claiming a request key.

    Attributes:
        key: Principal-scoped idempotency key.
        token: Fencing token required to mutate the claim's state.
        state: Current durable state, including retryable prepared states.
        prepared_record_id: Existing prepared transaction record, if present.
    """
    key: str
    token: str
    state: str
    prepared_record_id: int | None


@dataclass(frozen=True)
class PreparedBroadcast:
    """Exact transaction material persisted before a Bitcoin broadcast attempt.

    Keeping the raw transaction and expected txid together lets retries reconcile
    the original spend rather than rebuilding or broadcasting different bytes.

    Attributes:
        record_id: Primary key of the durable transaction record.
        wallet: Bitcoin Core wallet used to fund the transaction.
        request_hash: Fingerprint binding the record to the original request.
        outputs: Normalized destination/value pairs, with values in satoshis.
        psbt: Funded, signed/finalized PSBT retained for recovery and input cleanup.
        raw_tx: Exact serialized transaction submitted to Bitcoin Core.
        expected_txid: Transaction ID decoded before the broadcast attempt.
        fee_sats: Network fee estimated by Core, in satoshis.
        total_output_sats: Sum of requested output values, in satoshis.
    """
    record_id: int
    wallet: str
    request_hash: str
    outputs: list[dict[str, Any]]
    psbt: str
    raw_tx: str
    expected_txid: str
    fee_sats: int
    total_output_sats: int


class CohesionStore:
    """SQLite persistence for idempotency claims and financial transaction records.

    Writes are serialized within the process and claim transitions use SQLite
    transactions so worker ownership, replay state, and prepared broadcasts stay
    consistent across restarts.
    """

    def __init__(self, path: str, idempotency_ttl_seconds: int, claim_lease_seconds: int = 300) -> None:
        """Create the private state database and initialize/migrate its schema."""
        self._path = path
        self._ttl = idempotency_ttl_seconds
        self._claim_lease_seconds = max(5, int(claim_lease_seconds))
        self._lock = threading.RLock()
        self._ensure_parent()
        self._init_db()

    def get_replay(self, key: str, scope: str, request_hash: str) -> tuple[int, dict[str, Any]] | None:
        """Return a completed response or reject reuse for different request content."""
        now = int(time.time())
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT scope, request_hash, status_code, response_json, state
                FROM idempotency_records
                WHERE key = ? AND expires_at > ?
                """,
                (key, now),
            ).fetchone()
        if row is None:
            return None
        if row["scope"] != scope or row["request_hash"] != request_hash:
            raise ApiError(
                409,
                "IDEMPOTENCY_KEY_REUSED",
                "Idempotency-Key was already used for a different request.",
            )
        if row["state"] not in {"SUCCEEDED", "FAILED_FINAL"}:
            return None
        return int(row["status_code"]), json.loads(row["response_json"])

    def claim_idempotent(
        self,
        key: str,
        scope: str,
        request_hash: str,
    ) -> IdempotencyClaim | IdempotencyReplay:
        """Atomically claim a key, replay a completed result, or reject concurrent use.

        Expired records are replaced. A live lease blocks a second worker; an
        expired lease can be taken over with a fresh fencing token while retaining
        any persisted prepared transaction reference.
        """
        now = int(time.time())
        claim_token = str(uuid.uuid4())
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT scope, request_hash, status_code, response_json, state,
                           claim_token, lease_expires_at, expires_at, prepared_record_id
                    FROM idempotency_records
                    WHERE key = ?
                    """,
                    (key,),
                ).fetchone()

                if row is not None and int(row["expires_at"]) <= now:
                    conn.execute("DELETE FROM idempotency_records WHERE key = ?", (key,))
                    row = None

                if row is None:
                    conn.execute(
                        """
                        INSERT INTO idempotency_records(
                            key, scope, request_hash, status_code, response_json,
                            created_at, expires_at, claim_token, claimed_at,
                            lease_expires_at, state
                        )
                        VALUES(?, ?, ?, -1, '{}', ?, ?, ?, ?, ?, 'CLAIMED')
                        """,
                        (
                            key,
                            scope,
                            request_hash,
                            now,
                            now + self._ttl,
                            claim_token,
                            now,
                            now + self._claim_lease_seconds,
                        ),
                    )
                    conn.execute("COMMIT")
                    return IdempotencyClaim(key, claim_token, "CLAIMED", None)

                if row["scope"] != scope or row["request_hash"] != request_hash:
                    raise ApiError(
                        409,
                        "IDEMPOTENCY_KEY_REUSED",
                        "Idempotency-Key was already used for a different request.",
                    )

                state = str(row["state"])
                if state in {"SUCCEEDED", "FAILED_FINAL"}:
                    conn.execute("COMMIT")
                    return IdempotencyReplay(
                        int(row["status_code"]),
                        json.loads(row["response_json"]),
                    )

                lease_expires_at = int(row["lease_expires_at"] or 0)
                if row["claim_token"] and lease_expires_at > now:
                    raise ApiError(
                        409,
                        "IDEMPOTENCY_IN_PROGRESS",
                        "A request with this Idempotency-Key is still in progress.",
                    )

                updated = conn.execute(
                    """
                    UPDATE idempotency_records
                    SET claim_token = ?, claimed_at = ?, lease_expires_at = ?, expires_at = ?
                    WHERE key = ?
                      AND request_hash = ?
                      AND (claim_token IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= ?)
                    """,
                    (
                        claim_token,
                        now,
                        now + self._claim_lease_seconds,
                        now + self._ttl,
                        key,
                        request_hash,
                        now,
                    ),
                )
                if updated.rowcount != 1:
                    raise ApiError(
                        409,
                        "IDEMPOTENCY_IN_PROGRESS",
                        "A request with this Idempotency-Key is still in progress.",
                    )
                conn.execute("COMMIT")
                prepared_record_id = row["prepared_record_id"]
                return IdempotencyClaim(
                    key,
                    claim_token,
                    state,
                    int(prepared_record_id) if prepared_record_id is not None else None,
                )
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def store_response(
        self,
        key: str,
        scope: str,
        request_hash: str,
        claim_token: str,
        status_code: int,
        response: dict[str, Any],
    ) -> None:
        """Persist a completed HTTP response only if the caller still owns the claim."""
        now = int(time.time())
        expires_at = now + self._ttl
        payload = json.dumps(response, sort_keys=True, separators=(",", ":"))
        with self._lock, self._connect() as conn:
            updated = conn.execute(
                """
                UPDATE idempotency_records
                SET status_code = ?, response_json = ?, expires_at = ?, state = 'SUCCEEDED',
                    settled_at = ?, lease_expires_at = NULL, claim_token = NULL, last_error = NULL
                WHERE key = ? AND scope = ? AND request_hash = ? AND claim_token = ?
                  AND state NOT IN ('SUCCEEDED', 'FAILED_FINAL')
                """,
                (
                    int(status_code),
                    payload,
                    expires_at,
                    now,
                    key,
                    scope,
                    request_hash,
                    claim_token,
                ),
            )
            if updated.rowcount != 1:
                raise self._lost_claim()

    def save_prepared_broadcast(
        self,
        *,
        key: str,
        claim_token: str,
        request_hash: str,
        wallet: str,
        outputs: list[dict[str, Any]],
        psbt: str,
        raw_tx: str,
        expected_txid: str,
        fee_sats: int,
        total_output_sats: int,
        network: str,
    ) -> PreparedBroadcast:
        """Persist the exact funded/finalized transaction before first broadcast.

        The claim token and request hash are checked transactionally. A retry that
        already has a prepared record receives that record instead of inserting
        another transaction.
        """
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                owner = conn.execute(
                    """
                    SELECT request_hash, claim_token, state, prepared_record_id
                    FROM idempotency_records WHERE key = ?
                    """,
                    (key,),
                ).fetchone()
                if (
                    owner is None
                    or owner["claim_token"] != claim_token
                    or owner["request_hash"] != request_hash
                ):
                    raise self._lost_claim()
                if owner["prepared_record_id"] is not None:
                    prepared = self._load_prepared_by_id(
                        conn,
                        int(owner["prepared_record_id"]),
                        request_hash,
                    )
                    conn.execute("COMMIT")
                    return prepared
                if owner["state"] != "CLAIMED":
                    raise self._lost_claim()

                cursor = conn.execute(
                    """
                    INSERT INTO transaction_records(
                      wallet, kind, request_hash, idempotency_key, outputs_json,
                      psbt, raw_tx, txid, status, metadata_json,
                      created_at, updated_at, network
                    )
                    VALUES(?, 'broadcast', ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?)
                    """,
                    (
                        wallet,
                        request_hash,
                        key,
                        json.dumps(outputs, sort_keys=True, separators=(",", ":")),
                        psbt,
                        raw_tx,
                        expected_txid,
                        json.dumps(
                            {
                                "feeSats": int(fee_sats),
                                "totalOutputSats": int(total_output_sats),
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        now,
                        now,
                        network,
                    ),
                )
                record_id = int(cursor.lastrowid)
                updated = conn.execute(
                    """
                    UPDATE idempotency_records
                    SET state = 'PREPARED', prepared_record_id = ?, payment_hash = ?,
                        lease_expires_at = ?, last_error = NULL
                    WHERE key = ? AND claim_token = ? AND state = 'CLAIMED'
                    """,
                    (
                        record_id,
                        expected_txid,
                        now + self._claim_lease_seconds,
                        key,
                        claim_token,
                    ),
                )
                if updated.rowcount != 1:
                    raise self._lost_claim()
                conn.execute("COMMIT")
                return PreparedBroadcast(
                    record_id,
                    wallet,
                    request_hash,
                    list(outputs),
                    psbt,
                    raw_tx,
                    expected_txid,
                    int(fee_sats),
                    int(total_output_sats),
                )
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def load_prepared_broadcast(self, key: str, request_hash: str) -> PreparedBroadcast | None:
        """Load retry material for this idempotency key and matching request body."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT prepared_record_id FROM idempotency_records
                WHERE key = ? AND request_hash = ?
                """,
                (key, request_hash),
            ).fetchone()
            if row is None or row["prepared_record_id"] is None:
                return None
            return self._load_prepared_by_id(conn, int(row["prepared_record_id"]), request_hash)

    def mark_broadcast(self, key: str, claim_token: str, txid: str) -> None:
        """Record a confirmed broadcast transition on the claim and transaction row."""
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                """
                UPDATE idempotency_records
                SET state = 'BROADCAST', payment_hash = ?, lease_expires_at = ?, last_error = NULL
                WHERE key = ? AND claim_token = ? AND payment_hash = ?
                  AND state IN ('PREPARED', 'UNKNOWN', 'BROADCAST')
                """,
                (txid, now + self._claim_lease_seconds, key, claim_token, txid),
            )
            if updated.rowcount != 1:
                conn.execute("ROLLBACK")
                raise self._lost_claim()
            conn.execute(
                """
                UPDATE transaction_records SET status = 'broadcast', updated_at = ?
                WHERE id = (SELECT prepared_record_id FROM idempotency_records WHERE key = ?)
                """,
                (now, key),
            )
            conn.execute("COMMIT")

    def mark_failed(
        self,
        key: str,
        claim_token: str,
        status_code: int,
        error_code: str,
        message: str,
    ) -> None:
        """Persist a terminal failure for a claim that has not prepared a broadcast."""
        now = int(time.time())
        payload = json.dumps(
            {"success": False, "errorCode": error_code, "message": message},
            sort_keys=True, separators=(",", ":"),
        )
        with self._lock, self._connect() as conn:
            updated = conn.execute(
                """
                UPDATE idempotency_records
                SET status_code = ?, response_json = ?, state = 'FAILED_FINAL', settled_at = ?,
                    lease_expires_at = NULL, claim_token = NULL, last_error = ?
                WHERE key = ? AND claim_token = ? AND state = 'CLAIMED'
                """,
                (int(status_code), payload, now, message[:500], key, claim_token),
            )
            if updated.rowcount != 1:
                raise self._lost_claim()

    def mark_prepared_failed(
        self,
        key: str,
        claim_token: str,
        status_code: int,
        error_code: str,
        message: str,
    ) -> None:
        """Persist terminal rejection after preparation and mark its record rejected."""
        now = int(time.time())
        payload = json.dumps(
            {"success": False, "errorCode": error_code, "message": message},
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                """
                UPDATE idempotency_records
                SET status_code = ?, response_json = ?, state = 'FAILED_FINAL', settled_at = ?,
                    lease_expires_at = NULL, claim_token = NULL, last_error = ?
                WHERE key = ? AND claim_token = ? AND state IN ('PREPARED', 'UNKNOWN')
                """,
                (int(status_code), payload, now, message[:500], key, claim_token),
            )
            if updated.rowcount != 1:
                conn.execute("ROLLBACK")
                raise self._lost_claim()
            conn.execute(
                """
                UPDATE transaction_records SET status = 'rejected', updated_at = ?
                WHERE id = (SELECT prepared_record_id FROM idempotency_records WHERE key = ?)
                """,
                (now, key),
            )
            conn.execute("COMMIT")

    def mark_unknown(self, key: str, claim_token: str, txid: str, error: str) -> None:
        """Retain prepared transaction state when the broadcast outcome is uncertain."""
        now = int(time.time())
        with self._lock, self._connect() as conn:
            updated = conn.execute(
                """
                UPDATE idempotency_records
                SET state = 'UNKNOWN', payment_hash = ?, lease_expires_at = ?, last_error = ?
                WHERE key = ? AND claim_token = ? AND payment_hash = ?
                  AND state IN ('PREPARED', 'BROADCAST', 'UNKNOWN')
                """,
                (
                    txid,
                    now + self._claim_lease_seconds,
                    (error or "Bitcoin broadcast result is unknown")[:500],
                    key,
                    claim_token,
                    txid,
                ),
            )
            if updated.rowcount != 1:
                raise self._lost_claim()

    def heartbeat_claim(self, key: str, claim_token: str) -> bool:
        """Extend an active claim lease if the supplied fencing token still owns it."""
        now = int(time.time())
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE idempotency_records SET lease_expires_at = ?
                WHERE key = ? AND claim_token = ?
                  AND state IN ('CLAIMED', 'PREPARED', 'BROADCAST', 'UNKNOWN')
                """,
                (now + self._claim_lease_seconds, key, claim_token),
            )
            return cursor.rowcount > 0

    def idempotency_state(self, key: str) -> dict[str, Any] | None:
        """Return operational state for an idempotency key without its cached body."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT state, payment_hash, prepared_record_id, claim_token,
                       lease_expires_at, last_error
                FROM idempotency_records WHERE key = ?
                """,
                (key,),
            ).fetchone()
        return dict(row) if row is not None else None

    def _load_prepared_by_id(
        self,
        conn: sqlite3.Connection,
        record_id: int,
        request_hash: str,
    ) -> PreparedBroadcast:
        """Reconstruct prepared broadcast data and reject incomplete/corrupt rows."""
        row = conn.execute(
            """
            SELECT id, wallet, request_hash, outputs_json, psbt, raw_tx, txid, metadata_json
            FROM transaction_records WHERE id = ? AND kind = 'broadcast'
            """,
            (record_id,),
        ).fetchone()
        if (
            row is None
            or row["request_hash"] != request_hash
            or not row["psbt"]
            or not row["raw_tx"]
            or not row["txid"]
        ):
            raise ApiError(
                500,
                "PREPARED_BROADCAST_CORRUPT",
                "Persisted Bitcoin broadcast state is incomplete; manual reconciliation is required.",
            )
        metadata = json.loads(row["metadata_json"] or "{}")
        return PreparedBroadcast(
            int(row["id"]),
            str(row["wallet"]),
            str(row["request_hash"]),
            json.loads(row["outputs_json"]),
            str(row["psbt"]),
            str(row["raw_tx"]),
            str(row["txid"]),
            int(metadata.get("feeSats", 0)),
            int(metadata.get("totalOutputSats", 0)),
        )

    @staticmethod
    def _lost_claim() -> ApiError:
        """Create the consistent conflict response used when fencing fails."""
        return ApiError(
            409,
            "IDEMPOTENCY_CLAIM_LOST",
            "This worker no longer owns the idempotency claim.",
        )

    def record_transaction(
        self,
        *,
        wallet: str,
        kind: str,
        request_hash: str,
        idempotency_key: str | None,
        outputs: list[dict[str, Any]],
        psbt: str | None = None,
        raw_tx: str | None = None,
        txid: str | None = None,
        status: str,
        metadata: dict[str, Any] | None = None,
        network: str = "",
    ) -> int:
        """Insert a transaction/PSBT record and return its generated database ID."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO transaction_records(
                  wallet, kind, request_hash, idempotency_key, outputs_json,
                  psbt, raw_tx, txid, status, metadata_json, created_at, updated_at, network
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s','now'), strftime('%s','now'), ?)
                """,
                (
                    wallet,
                    kind,
                    request_hash,
                    idempotency_key,
                    json.dumps(outputs, sort_keys=True, separators=(",", ":")),
                    psbt,
                    raw_tx,
                    txid,
                    status,
                    json.dumps(metadata or {}, sort_keys=True, separators=(",", ":")),
                    network,
                ),
            )
            return int(cursor.lastrowid)

    def recent_transactions(self, wallet: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """List newest transaction records, optionally filtered to one wallet."""
        limit = max(1, min(limit, 100))
        query = """
            SELECT id, wallet, kind, txid, status, idempotency_key, metadata_json, created_at, updated_at
            FROM transaction_records
        """
        params: tuple[Any, ...] = ()
        if wallet:
            query += " WHERE wallet = ?"
            params = (wallet,)
        query += " ORDER BY id DESC LIMIT ?"
        params = (*params, limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "id": row["id"],
                "wallet": row["wallet"],
                "kind": row["kind"],
                "txid": row["txid"],
                "status": row["status"],
                "idempotencyKey": row["idempotency_key"],
                "metadata": json.loads(row["metadata_json"] or "{}"),
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            }
            for row in rows
        ]

    def summary(self) -> dict[str, Any]:
        """Return total transaction and idempotency row counts for diagnostics."""
        with self._connect() as conn:
            tx_count = conn.execute("SELECT count(*) AS value FROM transaction_records").fetchone()["value"]
            idem_count = conn.execute("SELECT count(*) AS value FROM idempotency_records").fetchone()["value"]
        return {"transactionRecords": tx_count, "idempotencyRecords": idem_count}

    def prune_expired(self) -> int:
        """Delete expired idempotency records and return the number removed."""
        now = int(time.time())
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM idempotency_records WHERE expires_at <= ?", (now,))
            return int(cursor.rowcount)

    def _ensure_parent(self) -> None:
        """Create private state storage, refusing database symlinks and broad modes."""
        path = Path(self._path).expanduser()
        if path.exists() and path.is_symlink():
            raise ValueError("BITCOIN_BACKEND_DB_PATH cannot be a symbolic link")
        parent = path.resolve(strict=False).parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.exists():
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        os.chmod(path, 0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a configured SQLite connection and always close it after use."""
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Enable durable SQLite settings and create or migrate required tables."""
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency_records (
                  key TEXT PRIMARY KEY,
                  scope TEXT NOT NULL,
                  request_hash TEXT NOT NULL,
                  status_code INTEGER NOT NULL,
                  response_json TEXT NOT NULL,
                  created_at INTEGER NOT NULL,
                  expires_at INTEGER NOT NULL,
                  claim_token TEXT,
                  claimed_at INTEGER,
                  lease_expires_at INTEGER,
                  state TEXT NOT NULL DEFAULT 'CLAIMED',
                  payment_hash TEXT,
                  payment_network TEXT NOT NULL DEFAULT '',
                  settled_at INTEGER,
                  prepared_record_id INTEGER,
                  last_error TEXT
                )
                """
            )
            idempotency_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(idempotency_records)").fetchall()
            }
            idempotency_migrations = {
                "claim_token": "TEXT",
                "claimed_at": "INTEGER",
                "lease_expires_at": "INTEGER",
                "state": "TEXT NOT NULL DEFAULT 'CLAIMED'",
                "payment_hash": "TEXT",
                "payment_network": "TEXT NOT NULL DEFAULT ''",
                "settled_at": "INTEGER",
                "prepared_record_id": "INTEGER",
                "last_error": "TEXT",
            }
            for column, definition in idempotency_migrations.items():
                if column not in idempotency_columns:
                    conn.execute(
                        f"ALTER TABLE idempotency_records ADD COLUMN {column} {definition}"
                    )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS transaction_records (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  wallet TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  request_hash TEXT NOT NULL,
                  idempotency_key TEXT,
                  outputs_json TEXT NOT NULL,
                  psbt TEXT,
                  raw_tx TEXT,
                  txid TEXT,
                  status TEXT NOT NULL,
                  metadata_json TEXT NOT NULL,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL,
                  network TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_wallet_status ON transaction_records(wallet, status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_txid ON transaction_records(txid)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_idem_expires ON idempotency_records(expires_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_idem_state ON idempotency_records(state)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_idem_lease ON idempotency_records(lease_expires_at)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_idem_prepared_record ON idempotency_records(prepared_record_id)"
            )
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self._path}{suffix}")
            if path.exists() and not path.is_symlink():
                os.chmod(path, 0o600)
