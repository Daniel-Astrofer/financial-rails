from __future__ import annotations

import base64
import io
import json
import urllib.error
from dataclasses import dataclass, field
from typing import Any

import pytest


PAYMENT_HASH = "a" * 64
SECOND_HASH = "b" * 64
INVOICE = "lntb1" + "p" * 80


@pytest.fixture(scope="module")
def lightning_modules():
    from src.adapters.inbound.http import app
    from src.config import settings as config
    from src.application import security
    from src.adapters.outbound.lightning import lnd

    return {
        "app": app,
        "config": config,
        "security": security,
        "lnd": lnd,
    }


@pytest.fixture
def bearer_headers():
    return {
        "Authorization": f"Bearer {'x' * 64}",
        "Idempotency-Key": "test-request-000001",
    }


def header_value(headers: dict[str, str], name: str) -> str | None:
    expected = name.lower()
    for key, value in headers.items():
        if key.lower() == expected:
            return value
    return None


@dataclass
class FakeLnd:
    invoices: dict[str, dict[str, Any]] = field(default_factory=dict)
    payments: dict[str, dict[str, Any]] = field(default_factory=dict)
    invoice_calls: int = 0
    payment_calls: int = 0

    def node_status(self):
        return {
            "alias": "kerosene-lnd",
            "synced_to_chain": True,
            "synced_to_graph": True,
            "block_height": 144,
            "wallet_confirmed_balance_sats": 100_000,
        }

    def list_channels(self):
        return {
            "channels": [
                {
                    "active": True,
                    "remote_pubkey": "02" + "b" * 64,
                    "channel_point": "c" * 64 + ":0",
                    "capacity_sats": 250_000,
                    "local_balance_sats": 125_000,
                    "remote_balance_sats": 120_000,
                }
            ]
        }

    def create_invoice(self, amount_sats, memo, expiry_seconds):
        self.invoice_calls += 1
        payment_hash = PAYMENT_HASH if self.invoice_calls == 1 else SECOND_HASH
        invoice = {
            "payment_hash": payment_hash,
            "payment_request": INVOICE,
            "amount_sats": amount_sats,
            "memo": memo,
            "expiry_seconds": expiry_seconds,
            "state": "OPEN",
            "settled": False,
        }
        self.invoices[payment_hash] = invoice
        return invoice

    def lookup_invoice(self, payment_hash):
        return self.invoices.get(
            payment_hash,
            {"payment_hash": payment_hash, "amount_sats": 0, "state": "UNKNOWN", "settled": False},
        )

    def pay_invoice(self, payment_request, fee_limit_sats, timeout_seconds, amount_sats=None):
        self.payment_calls += 1
        payment = {
            "payment_hash": PAYMENT_HASH,
            "payment_preimage": "secret-preimage",
            "payment_route": {"total_fees": str(fee_limit_sats)},
            "status": "submitted",
            "fee_limit_sats": fee_limit_sats,
        }
        self.payments[PAYMENT_HASH] = payment
        return payment

    def decode_invoice(self, payment_request):
        return {
            "payment_hash": PAYMENT_HASH,
            "num_satoshis": 50000,
            "timestamp": 0,
            "expiry": 3600,
            "destination": "03" + "c" * 64,
            "cltv_expiry": 144,
            "description": "test invoice",
            "network": "testnet",
        }

    def get_info(self):
        return {"chains": [{"chain": "bitcoin", "network": "testnet"}]}

    def lookup_payment(self, payment_hash):
        return self.payments.get(payment_hash, {"payment_hash": payment_hash, "status": "unknown"})

    def settle(self, payment_hash: str, amount_paid_sats: int):
        self.invoices[payment_hash] = {
            **self.invoices[payment_hash],
            "amount_paid_sats": amount_paid_sats,
            "state": "SETTLED",
            "settled": True,
        }
        self.payments[payment_hash] = {"payment_hash": payment_hash, "status": "SUCCEEDED", "fee_sats": 2}


@pytest.fixture
def lightning_app(tmp_path, lightning_modules):
    Settings = lightning_modules["config"].Settings
    create_app = lightning_modules["app"].create_app
    lnd = FakeLnd()
    settings = Settings(
        api_token="x" * 64,
        read_token="x" * 64,
        write_token="x" * 64,
        admin_token="",
        lnd_rest_url="https://127.0.0.1:8080",
        lnd_macaroon_hex="00" * 32,
        network="testnet",
        sqlite_path=str(tmp_path / "lightning.sqlite3"),
        rate_limit_per_minute=1_000,
        max_body_bytes=4096,
        max_invoice_sats=100_000,
        max_payment_sats=100_000,
        max_fee_sats=5000,
        max_fee_ppm=500,
        max_daily_payment_sats=1_000_000,
        max_in_flight_sats=500_000,
        default_invoice_expiry_seconds=900,
        auth_disabled=False,
        production=False,
    )
    return create_app(settings, lnd).test_client(), lnd


def test_health_is_public_and_auth_required(lightning_app):
    client, _lnd = lightning_app

    assert client.get("/health").status_code == 200
    assert client.get("/v1/node/status").status_code == 401


def test_node_status_channels_and_security_headers(lightning_app, bearer_headers):
    client, _lnd = lightning_app

    status = client.get("/v1/node/status", headers=bearer_headers)
    channels = client.get("/v1/channels", headers=bearer_headers)

    assert status.status_code == 200
    assert status.headers["Cache-Control"] == "no-store"
    assert status.headers["X-Content-Type-Options"] == "nosniff"
    node = status.get_json()["node"]
    assert node["synced_to_chain"] is True
    assert node["synced_to_graph"] is True
    assert node["network"] == "testnet"
    ch = channels.get_json()
    assert ch["activeChannelCount"] >= 0
    assert "outboundLiquiditySats" in ch


def test_invoice_creation_idempotency_and_snapshot(lightning_app, bearer_headers):
    client, lnd = lightning_app
    headers = {**bearer_headers, "Idempotency-Key": "invoice-1"}

    first = client.post("/v1/invoices", json={"amount_sats": 2500, "memo": "coffee"}, headers=headers)
    second = client.post("/v1/invoices", json={"amount_sats": 2500, "memo": "coffee"}, headers=headers)
    snapshot = client.get("/v1/cohesion/snapshot", headers=bearer_headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.get_json() == second.get_json()
    assert lnd.invoice_calls == 1
    assert snapshot.get_json()["cohesion"]["lightning_events"] == 1


def test_invoice_settlement_simulation(lightning_app, bearer_headers):
    client, lnd = lightning_app

    created = client.post("/v1/invoices", json={"amount_sats": 5000, "memo": "settle-me"}, headers=bearer_headers)
    payment_hash = created.get_json()["invoice"]["payment_hash"]
    open_invoice = client.get(f"/v1/invoices/{payment_hash}", headers=bearer_headers)
    lnd.settle(payment_hash, amount_paid_sats=5000)
    settled_invoice = client.get(f"/v1/invoices/{payment_hash}", headers=bearer_headers)

    assert open_invoice.get_json()["invoice"]["state"] == "OPEN"
    assert settled_invoice.get_json()["invoice"]["state"] == "SETTLED"
    assert settled_invoice.get_json()["invoice"]["settled"] is True


def test_payment_submission_redacts_sensitive_event_metadata(lightning_app, bearer_headers):
    client, lnd = lightning_app
    headers = {**bearer_headers, "Idempotency-Key": "payment-request-0001"}

    response = client.post(
        "/v1/payments",
        json={"payment_request": INVOICE, "fee_limit_sats": 10, "timeout_seconds": 30},
        headers=headers,
    )
    payment = client.get(f"/v1/payments/{PAYMENT_HASH}", headers=bearer_headers)
    snapshot = client.get("/v1/cohesion/snapshot", headers=bearer_headers).get_json()["cohesion"]

    assert response.status_code == 202, response.get_json()
    assert payment.get_json()["payment"]["status"] == "submitted"
    assert lnd.payment_calls == 1
    assert snapshot["recent_events"][0]["event_type"] == "payment_submitted"
    assert "payment_request" not in snapshot["recent_events"][0]["metadata"]
    assert "payment_preimage" not in snapshot["recent_events"][0]["metadata"]


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"amount_sats": 0}, "invalid_amount"),
        ({"amount_sats": True}, "invalid_amount"),
        ({"amount_sats": 100_001}, "amount_too_large"),
        ({"amount_sats": 1000, "memo": "x" * 257}, "invalid_memo"),
        ({"amount_sats": 1000, "expiry_seconds": 59}, "invalid_integer"),
    ],
)
def test_invoice_validation_edges(lightning_app, bearer_headers, payload, code):
    client, _lnd = lightning_app

    response = client.post("/v1/invoices", json=payload, headers=bearer_headers)

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == code


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"payment_request": "not-an-invoice"}, "invalid_invoice"),
        ({"payment_request": INVOICE, "fee_limit_sats": 0}, "invalid_integer"),
        ({"payment_request": INVOICE, "timeout_seconds": 601}, "invalid_integer"),
        ({"payment_request": INVOICE, "fee_limit_sats": True}, "invalid_integer"),
    ],
)
def test_payment_validation_edges(lightning_app, bearer_headers, payload, code):
    client, _lnd = lightning_app

    response = client.post("/v1/payments", json=payload, headers=bearer_headers)

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == code


def test_idempotency_conflict_and_content_type(lightning_app, bearer_headers):
    client, _lnd = lightning_app
    headers = {**bearer_headers, "Idempotency-Key": "invoice-conflict"}

    first = client.post("/v1/invoices", json={"amount_sats": 1000}, headers=headers)
    second = client.post("/v1/invoices", json={"amount_sats": 2000}, headers=headers)
    wrong_type = client.post("/v1/invoices", data="amount_sats=1", headers=bearer_headers)

    assert first.status_code == 201
    assert second.status_code == 409
    assert wrong_type.status_code == 415


class RestResponse:
    def __init__(self, payload: Any):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self, _limit: int):
        return json.dumps(self.payload).encode()


def test_lnd_client_creates_invoice_from_base64_hash(monkeypatch, tmp_path, lightning_modules):
    Settings = lightning_modules["config"].Settings
    LndClient = lightning_modules["lnd"].LndClient
    calls = []

    def fake_urlopen(request, timeout, context=None):
        calls.append((request.full_url, request.headers, json.loads(request.data.decode())))
        return RestResponse({"r_hash": base64.b64encode(bytes.fromhex(PAYMENT_HASH)).decode(), "payment_request": INVOICE})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = LndClient(
        Settings(
            api_token="x" * 64,
            read_token="x" * 64,
            write_token="x" * 64,
            admin_token="",
            lnd_rest_url="https://lnd.local:8080/",
            lnd_macaroon_hex="ab" * 32,
            network="testnet",
            sqlite_path=str(tmp_path / "unused.sqlite3"),
        )
    )

    invoice = client.create_invoice(1234, "memo", 600)

    assert invoice["payment_hash"] == PAYMENT_HASH
    assert calls[0][0] == "https://lnd.local:8080/v1/invoices"
    assert header_value(calls[0][1], "Grpc-Metadata-macaroon") == "ab" * 32
    assert calls[0][2]["value"] == "1234"


def test_lnd_client_status_maps_numeric_strings_and_caches(monkeypatch, tmp_path, lightning_modules):
    Settings = lightning_modules["config"].Settings
    LndClient = lightning_modules["lnd"].LndClient
    responses = iter(
        [
            {"identity_pubkey": "02" + "a" * 64, "alias": "node", "block_height": "7", "num_active_channels": "2"},
            {"confirmed_balance": "1000"},
            {"local_balance": {"sat": "600"}, "remote_balance": {"sat": "400"}},
        ]
    )
    calls = []

    def fake_urlopen(request, timeout, context=None):
        calls.append(request.full_url)
        return RestResponse(next(responses))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = LndClient(
        Settings(
            api_token="x" * 64,
            read_token="x" * 64,
            write_token="x" * 64,
            admin_token="",
            lnd_rest_url="https://lnd.local:8080",
            lnd_macaroon_hex="ab" * 32,
            network="testnet",
            sqlite_path=str(tmp_path / "unused.sqlite3"),
            status_cache_seconds=60,
        )
    )

    first = client.node_status()
    second = client.node_status()

    assert first == second
    assert first["block_height"] == 7
    assert first["channel_local_balance_sats"] == 600
    assert len(calls) == 3


def test_lnd_client_maps_http_and_network_errors(monkeypatch, tmp_path, lightning_modules):
    Settings = lightning_modules["config"].Settings
    LndClient = lightning_modules["lnd"].LndClient
    ApiError = lightning_modules["security"].ApiError
    client = LndClient(
        Settings(
            api_token="x" * 64,
            read_token="x" * 64,
            write_token="x" * 64,
            admin_token="",
            lnd_rest_url="https://lnd.local:8080",
            lnd_macaroon_hex="ab" * 32,
            network="testnet",
            sqlite_path=str(tmp_path / "unused.sqlite3"),
        )
    )

    def http_error(_request, timeout=None, context=None):
        raise urllib.error.HTTPError("https://lnd", 400, "bad", {}, io.BytesIO(b'{"error":"invoice expired"}'))

    monkeypatch.setattr("urllib.request.urlopen", http_error)
    with pytest.raises(ApiError) as error:
        client.lookup_invoice(PAYMENT_HASH)
    assert error.value.status_code == 502
    assert error.value.message == "invoice expired"

    def network_error(_request, timeout=None, context=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr("urllib.request.urlopen", network_error)
    with pytest.raises(ApiError) as unavailable:
        client.lookup_payment(PAYMENT_HASH)
    assert unavailable.value.status_code == 503
    assert unavailable.value.code == "lnd_unavailable"
