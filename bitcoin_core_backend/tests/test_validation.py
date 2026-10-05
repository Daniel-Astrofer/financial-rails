import unittest

from src.application.errors import ApiError
from src.application.validation import (
    btc_to_sats,
    normalize_outputs,
    parse_non_negative_int,
    parse_positive_int,
    request_fingerprint,
    sats_to_btc_string,
    validate_idempotency_key,
    validate_wallet_name,
)


class ValidationTests(unittest.TestCase):
    def test_wallet_names_reject_path_traversal(self):
        with self.assertRaises(ApiError):
            validate_wallet_name("../main")

    def test_wallet_names_accept_url_safe_names(self):
        self.assertEqual(validate_wallet_name("ops_wallet-01"), "ops_wallet-01")

    def test_satoshi_conversion_is_exact(self):
        self.assertEqual(sats_to_btc_string(123456789), "1.23456789")
        self.assertEqual(btc_to_sats("1.23456789"), 123456789)

    def test_normalize_outputs_merges_duplicates_and_sorts(self):
        body = {
            "outputs": [
                {"address": "bc1qtestaddress000000000000000000000000000001", "amountSats": 2},
                {"address": "bc1qtestaddress000000000000000000000000000001", "amountSats": 3},
                {"address": "bc1qtestaddress000000000000000000000000000002", "amountSats": "4"},
            ]
        }
        self.assertEqual(
            normalize_outputs(body, max_outputs=5, max_send_sats=100),
            [
                {"address": "bc1qtestaddress000000000000000000000000000001", "amountSats": 5},
                {"address": "bc1qtestaddress000000000000000000000000000002", "amountSats": 4},
            ],
        )

    def test_integer_parsing_rejects_fractional_values(self):
        for value in (1.1, "1.1", "1e2", " 1", True):
            with self.subTest(value=value):
                with self.assertRaises(ApiError):
                    parse_positive_int(value, "amountSats")
                with self.assertRaises(ApiError):
                    parse_non_negative_int(value, "confirmations")

    def test_integer_parsing_accepts_digit_strings(self):
        self.assertEqual(parse_positive_int("25", "amountSats"), 25)
        self.assertEqual(parse_non_negative_int("0", "confirmations"), 0)

    def test_idempotency_key_policy(self):
        self.assertEqual(validate_idempotency_key("request-000001"), "request-000001")
        with self.assertRaises(ApiError):
            validate_idempotency_key("short")

    def test_fingerprint_is_canonical(self):
        first = request_fingerprint("post", "/v1/demo", {"b": 2, "a": 1})
        second = request_fingerprint("POST", "/v1/demo", {"a": 1, "b": 2})
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
