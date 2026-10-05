from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if importlib.util.find_spec("flask") is None:
    raise unittest.SkipTest("Flask is not installed")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.adapters.inbound.http.app import create_app
from src.config.settings import Settings


PAYMENT_HASH = "a" * 64
# Valid BOLT11-like invoice for testing: 500 uBTC = 50000 sats, under test limit of 100000
INVOICE = "lnbc500u1" + "a" * 79


class FakeLndClient:
    def __init__(self):
        self.invoice_calls = 0
        self.payment_calls = 0
        self.decode_called = False

    def node_status(self):
        return {"alias": "kerosene-lnd", "synced_to_chain": True, "synced_to_graph": True, "block_height": 100}

    def list_channels(self):
        return {"channels": [{"active": True, "remote_pubkey": "02" + "b" * 64, "capacity_sats": 1000}]}

    def get_info(self):
        return {"chains": [{"chain": "bitcoin", "network": "mainnet"}]}

    def decode_invoice(self, payment_request):
        self.decode_called = True
        return {
            "payment_hash": PAYMENT_HASH,
            "num_satoshis": 50000,
            "timestamp": 0,
            "expiry": 3600,
            "destination": "03" + "c" * 64,
            "cltv_expiry": 144,
            "description": "test invoice",
            "network": "mainnet",
        }

    def create_invoice(self, amount_sats, memo, expiry_seconds):
        self.invoice_calls += 1
        return {
            "payment_hash": PAYMENT_HASH,
            "payment_request": INVOICE,
            "amount_sats": amount_sats,
            "memo": memo,
            "expiry_seconds": expiry_seconds,
        }

    def lookup_invoice(self, payment_hash):
        return {"payment_hash": payment_hash, "amount_sats": 2500, "state": "OPEN", "settled": False}

    def pay_invoice(self, payment_request, fee_limit_sats, timeout_seconds, amount_sats=None):
        self.payment_calls += 1
        return {
            "payment_hash": PAYMENT_HASH,
            "payment_error": "",
            "status": "submitted",
            "fee_limit_sats": fee_limit_sats,
        }

    def lookup_payment(self, payment_hash):
        return {"payment_hash": payment_hash, "status": "SUCCEEDED", "fee_sats": 2}


class LightningAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=True)
        token = "x" * 64  # 32 bytes entropy (hex)
        self.settings = Settings(
            api_token=token,
            read_token=token,
            write_token=token,
            admin_token="",
            network="mainnet",
            lnd_rest_url="https://127.0.0.1:8080",
            lnd_macaroon_hex="00" * 32,
            sqlite_path=self.tmp.name,
            rate_limit_per_minute=1000,
            max_invoice_sats=100_000,
            max_payment_sats=100_000,
            max_fee_sats=5000,
            max_fee_ppm=500,
            max_daily_payment_sats=1_000_000,
            max_in_flight_sats=500_000,
            auth_disabled=False,
            production=False,
        )
        self.lnd = FakeLndClient()
        self.client = create_app(self.settings, self.lnd).test_client()
        self.headers = {"Authorization": "Bearer " + "x" * 64}

    def test_health_is_public(self):
        response = self.client.get("/health")
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.get_json()["success"])

    def test_auth_required(self):
        response = self.client.get("/v1/node/status")
        self.assertEqual(401, response.status_code)

    def test_status_and_security_headers(self):
        response = self.client.get("/v1/node/status", headers=self.headers)
        body = response.get_json()
        self.assertEqual(200, response.status_code)
        self.assertTrue(body["node"]["synced_to_chain"])
        self.assertEqual(100, body["node"]["block_height"])
        self.assertEqual("mainnet", body["node"]["network"])
        self.assertEqual("no-store", response.headers["Cache-Control"])
        self.assertEqual("nosniff", response.headers["X-Content-Type-Options"])

    def test_create_invoice_idempotency(self):
        headers = {**self.headers, "Content-Type": "application/json", "Idempotency-Key": "invoice-1"}
        body = {"amount_sats": 2500, "memo": "coffee", "expiry_seconds": 600}
        first = self.client.post("/v1/invoices", json=body, headers=headers)
        second = self.client.post("/v1/invoices", json=body, headers=headers)
        self.assertEqual(201, first.status_code)
        self.assertEqual(201, second.status_code)
        self.assertEqual(first.get_json(), second.get_json())
        self.assertEqual(1, self.lnd.invoice_calls)

    def test_idempotency_conflict(self):
        headers = {**self.headers, "Content-Type": "application/json", "Idempotency-Key": "invoice-conflict"}
        first = self.client.post("/v1/invoices", json={"amount_sats": 1000}, headers=headers)
        second = self.client.post("/v1/invoices", json={"amount_sats": 2000}, headers=headers)
        self.assertEqual(201, first.status_code)
        self.assertEqual(409, second.status_code)

    def test_invoice_amount_validation(self):
        headers = {**self.headers, "Content-Type": "application/json", "Idempotency-Key": "invoice-amount-test"}
        response = self.client.post("/v1/invoices", json={"amount_sats": 100_001}, headers=headers)
        self.assertEqual(400, response.status_code)

    def test_pay_invoice_validation_and_audit_sanitization(self):
        headers = {**self.headers, "Content-Type": "application/json", "Idempotency-Key": "payment-request-0001"}
        response = self.client.post(
            "/v1/payments",
            json={"payment_request": INVOICE, "fee_limit_sats": 10, "timeout_seconds": 30},
            headers=headers,
        )
        self.assertEqual(202, response.status_code, response.get_json())
        self.assertEqual(1, self.lnd.payment_calls)

        snapshot = self.client.get("/v1/cohesion/snapshot", headers=self.headers).get_json()["cohesion"]
        self.assertEqual(1, snapshot["lightning_events"])
        metadata = snapshot["recent_events"][0]["metadata"]
        self.assertNotIn("payment_request", metadata)
        self.assertNotIn("payment_preimage", metadata)

    def test_rejects_invalid_bolt11(self):
        headers = {**self.headers, "Content-Type": "application/json", "Idempotency-Key": "bolt11-test"}
        response = self.client.post("/v1/payments", json={"payment_request": "not-an-invoice"}, headers=headers)
        self.assertEqual(400, response.status_code)

    def test_lookup_payment_hash_validation(self):
        response = self.client.get("/v1/payments/nothex", headers=self.headers)
        self.assertEqual(400, response.status_code)

    def test_timeout_retry_reconciles_without_second_payment(self):
        calls = 0

        def timeout(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise TimeoutError("LND response timed out")

        self.lnd.pay_invoice = timeout
        headers = {
            **self.headers,
            "Content-Type": "application/json",
            "Idempotency-Key": "payment-timeout-0001",
        }
        body = {"payment_request": INVOICE, "fee_limit_sats": 10, "timeout_seconds": 30}
        with patch("src.adapters.outbound.persistence.cohesion.time.time", return_value=1000):
            first = self.client.post("/v1/payments", json=body, headers=headers)

        self.assertEqual(504, first.status_code)
        self.lnd.lookup_payment = lambda payment_hash: {
            "payment_hash": payment_hash,
            "status": "SUCCEEDED",
            "fee_sats": 2,
        }
        with patch("src.adapters.outbound.persistence.cohesion.time.time", return_value=1301):
            second = self.client.post("/v1/payments", json=body, headers=headers)

        self.assertEqual(202, second.status_code, second.get_json())
        self.assertEqual("SUCCEEDED", second.get_json()["payment"]["status"])
        self.assertEqual(1, calls)


if __name__ == "__main__":
    unittest.main()
