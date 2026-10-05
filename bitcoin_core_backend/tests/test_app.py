import importlib.util
import tempfile
import unittest
from unittest.mock import patch

if importlib.util.find_spec("flask") is None or importlib.util.find_spec("requests") is None:
    raise unittest.SkipTest("Flask test dependencies are not installed")

from src.adapters.inbound.http.app import create_app
from src.config.settings import AppConfig


def app_config(state_db_path):
    return AppConfig(
        rpc_url="http://127.0.0.1:18443",
        rpc_user="user",
        rpc_password="password",
        default_wallet="kerosene",
        chain="regtest",
        api_keys=frozenset(),
        read_api_keys=frozenset(),
        write_api_keys=frozenset(),
        admin_token="",
        auth_disabled=True,
        allow_wallet_create=False,
        allow_broadcast=False,
        connect_timeout_seconds=0.1,
        read_timeout_seconds=0.1,
        rpc_pool_size=1,
        max_content_length=1024,
        max_outputs_per_tx=4,
        max_send_sats=100_000,
        default_min_confirmations=1,
        idempotency_ttl_seconds=3600,
        state_db_path=state_db_path,
        rate_limit_per_minute=100,
        rate_limit_backend="memory",
        redis_url="",
    )

def create_test_app(state_db_path):
    with patch(
        "src.adapters.outbound.bitcoin_core.rpc.BitcoinRPCClient.call",
        return_value={"chain": "regtest"},
    ):
        return create_app(app_config(state_db_path))


class AppRouteTests(unittest.TestCase):
    def test_malformed_json_returns_structured_400(self):
        with tempfile.NamedTemporaryFile() as tmp:
            app = create_test_app(tmp.name)

            response = app.test_client().post(
                "/v1/wallets",
                data=b'{"name":',
                content_type="application/json",
            )

        body = response.get_json()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(body["errorCode"], "INVALID_JSON")
        self.assertFalse(body["success"])

    def test_json_null_body_returns_structured_400(self):
        with tempfile.NamedTemporaryFile() as tmp:
            app = create_test_app(tmp.name)

            response = app.test_client().post(
                "/v1/wallets",
                data=b"null",
                content_type="application/json",
            )

        body = response.get_json()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(body["errorCode"], "INVALID_JSON")
        self.assertFalse(body["success"])

    def test_idempotent_replay_uses_current_request_id(self):
        with tempfile.NamedTemporaryFile() as tmp:
            app = create_test_app(tmp.name)
            client = app.test_client()

            with patch("src.adapters.outbound.bitcoin_core.rpc.BitcoinRPCClient.call", return_value=["kerosene"]) as call:
                first = client.post(
                    "/v1/wallets",
                    json={"name": "kerosene"},
                    headers={
                        "Idempotency-Key": "request-000001",
                        "X-Request-Id": "first-request",
                    },
                )
                second = client.post(
                    "/v1/wallets",
                    json={"name": "kerosene"},
                    headers={
                        "Idempotency-Key": "request-000001",
                        "X-Request-Id": "second-request",
                    },
                )

        second_body = second.get_json()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.headers["X-Request-Id"], "second-request")
        self.assertEqual(second_body["requestId"], "second-request")
        self.assertTrue(second_body["idempotentReplay"])
        self.assertEqual(call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
