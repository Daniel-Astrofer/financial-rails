import tempfile
import unittest
from unittest.mock import patch

from src.application.errors import ApiError
from src.adapters.outbound.persistence.store import CohesionStore, IdempotencyClaim


class CohesionStoreTests(unittest.TestCase):
    def test_idempotency_replay_and_conflict(self):
        with tempfile.NamedTemporaryFile() as tmp:
            store = CohesionStore(tmp.name, idempotency_ttl_seconds=3600)
            claim = store.claim_idempotent("request-000001", "POST:/v1/a", "hash-a")
            self.assertIsInstance(claim, IdempotencyClaim)
            store.store_response(
                "request-000001",
                "POST:/v1/a",
                "hash-a",
                claim.token,
                200,
                {"success": True},
            )

            self.assertEqual(
                store.get_replay("request-000001", "POST:/v1/a", "hash-a"),
                (200, {"success": True}),
            )
            with self.assertRaises(ApiError):
                store.get_replay("request-000001", "POST:/v1/a", "hash-b")

    def test_expired_idempotency_key_can_be_reused_for_new_scope(self):
        with tempfile.NamedTemporaryFile() as tmp:
            store = CohesionStore(tmp.name, idempotency_ttl_seconds=1)
            with patch("src.adapters.outbound.persistence.store.time.time", return_value=1000):
                first = store.claim_idempotent("request-000001", "POST:/v1/a", "hash-a")
                self.assertIsInstance(first, IdempotencyClaim)
                store.store_response(
                    "request-000001", "POST:/v1/a", "hash-a", first.token, 200, {"route": "a"}
                )

            with patch("src.adapters.outbound.persistence.store.time.time", return_value=1002):
                self.assertIsNone(store.get_replay("request-000001", "POST:/v1/a", "hash-a"))
                second = store.claim_idempotent("request-000001", "POST:/v1/b", "hash-b")
                self.assertIsInstance(second, IdempotencyClaim)
                store.store_response(
                    "request-000001", "POST:/v1/b", "hash-b", second.token, 201, {"route": "b"}
                )
                self.assertEqual(
                    store.get_replay("request-000001", "POST:/v1/b", "hash-b"),
                    (201, {"route": "b"}),
                )
                with self.assertRaises(ApiError):
                    store.get_replay("request-000001", "POST:/v1/a", "hash-a")

    def test_stale_owner_is_fenced_after_lease_takeover(self):
        with tempfile.NamedTemporaryFile() as tmp:
            store = CohesionStore(tmp.name, idempotency_ttl_seconds=3600, claim_lease_seconds=5)
            with patch("src.adapters.outbound.persistence.store.time.time", return_value=1000):
                first = store.claim_idempotent("key", "POST:/send", "hash")
            with patch("src.adapters.outbound.persistence.store.time.time", return_value=1006):
                second = store.claim_idempotent("key", "POST:/send", "hash")

            self.assertIsInstance(first, IdempotencyClaim)
            self.assertIsInstance(second, IdempotencyClaim)
            self.assertNotEqual(first.token, second.token)
            with self.assertRaises(ApiError) as lost:
                store.store_response(
                    "key", "POST:/send", "hash", first.token, 200, {"success": True}
                )
            self.assertEqual(lost.exception.code, "IDEMPOTENCY_CLAIM_LOST")

    def test_prepared_broadcast_survives_lease_takeover(self):
        expected_txid = "ab" * 32
        with tempfile.NamedTemporaryFile() as tmp:
            store = CohesionStore(tmp.name, idempotency_ttl_seconds=3600, claim_lease_seconds=5)
            with patch("src.adapters.outbound.persistence.store.time.time", return_value=1000):
                first = store.claim_idempotent("key", "POST:/send", "hash")
                prepared = store.save_prepared_broadcast(
                    key="key",
                    claim_token=first.token,
                    request_hash="hash",
                    wallet="main",
                    outputs=[{"address": "bcrt1qexample", "amountSats": 1000}],
                    psbt="cHNidP8BAAoCAAAAAQ==",
                    raw_tx="02000000000100",
                    expected_txid=expected_txid,
                    fee_sats=100,
                    total_output_sats=1000,
                    network="regtest",
                )
            with patch("src.adapters.outbound.persistence.store.time.time", return_value=1006):
                second = store.claim_idempotent("key", "POST:/send", "hash")

            recovered = store.load_prepared_broadcast("key", "hash")
            self.assertEqual(second.prepared_record_id, prepared.record_id)
            self.assertEqual(recovered.raw_tx, prepared.raw_tx)
            self.assertEqual(recovered.expected_txid, expected_txid)

    def test_transaction_summary(self):
        with tempfile.NamedTemporaryFile() as tmp:
            store = CohesionStore(tmp.name, idempotency_ttl_seconds=3600)
            record_id = store.record_transaction(
                wallet="main",
                kind="psbt",
                request_hash="hash",
                idempotency_key=None,
                outputs=[{"address": "bc1qexample", "amountSats": 1}],
                status="created",
            )
            self.assertGreater(record_id, 0)
            self.assertEqual(store.summary()["transactionRecords"], 1)
            self.assertEqual(store.recent_transactions("main")[0]["status"], "created")


if __name__ == "__main__":
    unittest.main()
