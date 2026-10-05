import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from src.application.security import ApiError
from src.adapters.outbound.persistence.cohesion import CohesionStore, IdempotencyClaim


class CohesionStoreTests(unittest.TestCase):
    def test_stale_submitted_owner_is_fenced(self):
        payment_hash = "ab" * 32
        with tempfile.NamedTemporaryFile() as tmp:
            store = CohesionStore(tmp.name, claim_lease_seconds=5)
            with patch("src.adapters.outbound.persistence.cohesion.time.time", return_value=1000):
                first = store.claim_idempotent(
                    "principal:key",
                    "fingerprint",
                    payment_hash=payment_hash,
                    network="regtest",
                )
                store.mark_submitted(
                    "principal:key",
                    "fingerprint",
                    first.token,
                    payment_hash,
                )
            with patch("src.adapters.outbound.persistence.cohesion.time.time", return_value=1006):
                second = store.claim_idempotent(
                    "principal:key",
                    "fingerprint",
                    payment_hash=payment_hash,
                    network="regtest",
                )

            self.assertIsInstance(first, IdempotencyClaim)
            self.assertIsInstance(second, IdempotencyClaim)
            self.assertEqual(second.state, "SUBMITTED")
            self.assertEqual(second.payment_hash, payment_hash)
            with self.assertRaises(ApiError) as lost:
                store.save_idempotent(
                    "principal:key",
                    "fingerprint",
                    first.token,
                    {"success": True},
                    202,
                )
            self.assertEqual(lost.exception.code, "idempotency_claim_lost")

    def test_database_file_is_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/state.sqlite3"
            CohesionStore(path)
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
