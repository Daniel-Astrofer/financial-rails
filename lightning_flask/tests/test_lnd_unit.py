import unittest
from unittest.mock import patch

from src.config.settings import Settings
from src.adapters.outbound.lightning.lnd import LndClient


def settings() -> Settings:
    return Settings(
        api_token="x" * 64,
        read_token="x" * 64,
        write_token="x" * 64,
        admin_token="",
        network="regtest",
        lnd_rest_url="https://127.0.0.1:8080",
        lnd_macaroon_hex="00" * 32,
    )


class LndClientTests(unittest.TestCase):
    def test_amountless_invoice_forwards_explicit_amount(self):
        client = LndClient(settings())
        payment_hash = "ab" * 32
        with patch.object(
            client,
            "post",
            return_value={"payment_hash": payment_hash, "status": "SUCCEEDED"},
        ) as post:
            client.pay_invoice("lnbcrt1amountless", 10, 30, amount_sats=2500)

        payload = post.call_args.args[1]
        self.assertEqual(payload["amt"], "2500")

    def test_lookup_payment_matches_exact_hash(self):
        client = LndClient(settings())
        expected = "ab" * 32
        other = "cd" * 32
        with patch.object(
            client,
            "get",
            return_value={
                "payments": [
                    {"payment_hash": other, "status": "SUCCEEDED"},
                    {"payment_hash": expected, "status": "IN_FLIGHT", "fee_sat": "7"},
                ],
                "last_index_offset": "2",
            },
        ):
            payment = client.lookup_payment(expected)

        self.assertEqual(payment["payment_hash"], expected)
        self.assertEqual(payment["status"], "IN_FLIGHT")
        self.assertEqual(payment["fee_sats"], 7)


if __name__ == "__main__":
    unittest.main()
