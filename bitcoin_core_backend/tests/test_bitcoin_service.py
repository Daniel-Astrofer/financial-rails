import tempfile
import unittest
from unittest.mock import patch

from src.config.settings import AppConfig
from src.application.errors import ApiError, RpcError
from src.adapters.outbound.persistence.store import CohesionStore, IdempotencyClaim
from src.application.service import BitcoinBackendService


class AmbiguousBroadcastRpc:
    def __init__(self, txid: str) -> None:
        self.txid = txid
        self.known = False
        self.calls: list[str] = []

    def call(self, method, params=None, wallet=None):
        self.calls.append(method)
        if method == "validateaddress":
            return {"isvalid": True}
        if method == "walletcreatefundedpsbt":
            return {"psbt": "funded-psbt", "fee": "0.00000100", "changepos": 1}
        if method == "walletprocesspsbt":
            return {"psbt": "signed-psbt", "complete": True}
        if method == "finalizepsbt":
            return {"hex": "02000000000100", "complete": True}
        if method == "decoderawtransaction":
            return {"txid": self.txid}
        if method == "testmempoolaccept":
            return [{"allowed": True}]
        if method == "getmempoolentry":
            if self.known:
                return {"vsize": 100}
            raise RpcError(method, -5, "not in mempool")
        if method == "gettransaction":
            if self.known:
                return {"txid": self.txid}
            raise RpcError(method, -5, "unknown transaction")
        if method == "sendrawtransaction":
            raise RpcError(method, None, "request timed out", 504)
        raise AssertionError(f"Unexpected RPC method: {method}")


def config(db_path: str) -> AppConfig:
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
        allow_broadcast=True,
        connect_timeout_seconds=0.1,
        read_timeout_seconds=0.1,
        rpc_pool_size=1,
        max_content_length=1024,
        max_outputs_per_tx=4,
        max_send_sats=100_000,
        default_min_confirmations=1,
        idempotency_ttl_seconds=3600,
        state_db_path=db_path,
        rate_limit_per_minute=100,
        rate_limit_backend="memory",
        redis_url="",
    )


class BitcoinBackendServiceTests(unittest.TestCase):
    def test_ambiguous_broadcast_reuses_persisted_raw_transaction(self):
        txid = "ab" * 32
        body = {
            "confirmBroadcast": True,
            "outputs": [{"address": "bcrt1qexample000000", "amountSats": 10_000}],
        }
        with tempfile.NamedTemporaryFile() as tmp:
            store = CohesionStore(tmp.name, idempotency_ttl_seconds=3600, claim_lease_seconds=5)
            rpc = AmbiguousBroadcastRpc(txid)
            service = BitcoinBackendService(config(tmp.name), rpc, store)

            with patch("src.adapters.outbound.persistence.store.time.time", return_value=1000):
                first = store.claim_idempotent("principal:key", "POST:/send", "request-hash")
                self.assertIsInstance(first, IdempotencyClaim)
                with self.assertRaises(ApiError) as ambiguous:
                    service.create_sign_and_send(
                        "kerosene",
                        body,
                        idempotency_key="principal:key",
                        request_hash="request-hash",
                        claim_token=first.token,
                    )

            self.assertEqual(ambiguous.exception.code, "BROADCAST_RESULT_UNKNOWN")
            self.assertEqual(store.idempotency_state("principal:key")["state"], "UNKNOWN")

            rpc.known = True
            with patch("src.adapters.outbound.persistence.store.time.time", return_value=1006):
                second = store.claim_idempotent("principal:key", "POST:/send", "request-hash")
                result = service.create_sign_and_send(
                    "kerosene",
                    body,
                    idempotency_key="principal:key",
                    request_hash="request-hash",
                    claim_token=second.token,
                )

            self.assertEqual(result["txid"], txid)
            self.assertEqual(rpc.calls.count("walletcreatefundedpsbt"), 1)
            self.assertEqual(rpc.calls.count("sendrawtransaction"), 1)
            self.assertEqual(store.idempotency_state("principal:key")["state"], "BROADCAST")


if __name__ == "__main__":
    unittest.main()
