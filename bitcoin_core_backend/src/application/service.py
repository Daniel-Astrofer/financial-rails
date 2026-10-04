"""Bitcoin wallet workflows coordinating RPC, validation, and durable state."""

from __future__ import annotations

from typing import Any

from src.application.errors import ApiError, RpcError
from src.application.ports import BitcoinNodePort, BitcoinSettingsPort, IdempotencyPort
from src.application.validation import (
    btc_to_sats,
    normalize_outputs,
    parse_non_negative_int,
    request_fingerprint,
    sats_to_btc_string,
    validate_address_literal,
    validate_address_type,
    validate_label,
    validate_txid,
    validate_wallet_name,
)


class BitcoinBackendService:
    """Coordinate validated wallet operations, Bitcoin RPC, and durable state."""

    def __init__(self, config: BitcoinSettingsPort, rpc: BitcoinNodePort, store: IdempotencyPort) -> None:
        """Inject runtime settings, the Bitcoin RPC port, and the cohesion store."""
        self._config = config
        self._rpc = rpc
        self._store = store

    def node_status(self) -> dict[str, Any]:
        """Combine blockchain, network, and loaded-wallet status into one snapshot."""
        chain = self._rpc.call("getblockchaininfo")
        network = self._rpc.call("getnetworkinfo")
        wallets = self._rpc.call("listwallets")
        return {
            "chain": chain.get("chain"),
            "blocks": chain.get("blocks"),
            "headers": chain.get("headers"),
            "verificationProgress": chain.get("verificationprogress"),
            "initialBlockDownload": chain.get("initialblockdownload"),
            "pruned": chain.get("pruned"),
            "connections": network.get("connections"),
            "networkActive": network.get("networkactive"),
            "loadedWallets": wallets,
        }

    def open_wallet(self, body: dict[str, Any]) -> dict[str, Any]:
        """Load an existing wallet or create it when explicitly permitted.

        The request may opt into creation with ``createIfMissing`` and select
        private-key/blank-wallet behavior. Creation remains disabled unless the
        deployment enables the corresponding setting.
        """
        wallet = validate_wallet_name(body.get("name") or self._config.default_wallet)
        create_if_missing = bool(body.get("createIfMissing", False))
        disable_private_keys = bool(body.get("disablePrivateKeys", False))
        blank = bool(body.get("blank", False))

        loaded = self._rpc.call("listwallets")
        if wallet in loaded:
            return {"wallet": wallet, "loaded": True, "created": False}

        try:
            self._rpc.call("loadwallet", [wallet])
            return {"wallet": wallet, "loaded": True, "created": False}
        except RpcError as exc:
            if exc.rpc_code != -18 or not create_if_missing:
                raise

        if not self._config.allow_wallet_create:
            raise ApiError(
                403,
                "WALLET_CREATE_DISABLED",
                "Wallet creation is disabled. Set BITCOIN_BACKEND_ALLOW_WALLET_CREATE=true to enable it.",
            )
        result = self._rpc.call(
            "createwallet",
            [wallet, disable_private_keys, blank, "", True, True, True],
        )
        return {
            "wallet": wallet,
            "loaded": True,
            "created": True,
            "warning": result.get("warning") if isinstance(result, dict) else None,
        }

    def wallet_balance(self, wallet_name: str) -> dict[str, Any]:
        """Return wallet metadata and trusted/pending/immature balances in satoshis."""
        wallet = validate_wallet_name(wallet_name)
        balances = self._rpc.call("getbalances", wallet=wallet)
        info = self._rpc.call("getwalletinfo", wallet=wallet)
        mine = balances.get("mine", {})
        return {
            "wallet": wallet,
            "txCount": info.get("txcount"),
            "keypoolOldest": info.get("keypoololdest"),
            "privateKeysEnabled": bool(info.get("private_keys_enabled", True)),
            "avoidReuse": info.get("avoid_reuse"),
            "trustedSats": btc_to_sats(mine.get("trusted", 0)),
            "untrustedPendingSats": btc_to_sats(mine.get("untrusted_pending", 0)),
            "immatureSats": btc_to_sats(mine.get("immature", 0)),
        }

    def new_address(self, wallet_name: str, body: dict[str, Any]) -> dict[str, Any]:
        """Validate address options, request a new address, and verify it with Core."""
        wallet = validate_wallet_name(wallet_name)
        label = validate_label(body.get("label"), "")
        address_type = validate_address_type(body.get("addressType"))
        address = self._rpc.call("getnewaddress", [label, address_type], wallet=wallet)
        validation = self._rpc.call("validateaddress", [address])
        return {
            "wallet": wallet,
            "address": address,
            "addressType": address_type,
            "label": label,
            "isValid": bool(validation.get("isvalid")),
        }

    def list_utxos(self, wallet_name: str, query: dict[str, Any]) -> dict[str, Any]:
        """List bounded wallet UTXO data with BTC values converted to satoshis."""
        wallet = validate_wallet_name(wallet_name)
        min_conf = parse_non_negative_int(
            query.get("minConfirmations"),
            "minConfirmations",
            self._config.default_min_confirmations,
            9999999,
        )
        max_count = parse_non_negative_int(query.get("limit"), "limit", 100, 500)
        utxos = self._rpc.call("listunspent", [min_conf, 9999999, [], True], wallet=wallet)
        compact = []
        for utxo in utxos[:max_count]:
            compact.append(
                {
                    "txid": utxo.get("txid"),
                    "vout": utxo.get("vout"),
                    "address": utxo.get("address"),
                    "amountSats": btc_to_sats(utxo.get("amount", 0)),
                    "confirmations": utxo.get("confirmations"),
                    "spendable": utxo.get("spendable"),
                    "solvable": utxo.get("solvable"),
                    "safe": utxo.get("safe"),
                    "reused": utxo.get("reused", False),
                }
            )
        return {"wallet": wallet, "minConfirmations": min_conf, "utxos": compact}

    def create_psbt(
        self,
        wallet_name: str,
        body: dict[str, Any],
        *,
        idempotency_key: str | None,
        request_hash: str,
    ) -> dict[str, Any]:
        """Create a funded unsigned PSBT and persist its request/output metadata.

        This path does not sign or broadcast. The request fingerprint and
        idempotency key bind the durable transaction record to the original call.
        """
        funded = self._fund_psbt(wallet_name, body, lock_unspents=False)
        record_id = self._store.record_transaction(
            wallet=funded["wallet"],
            kind="psbt",
            request_hash=request_hash,
            idempotency_key=idempotency_key,
            outputs=funded["outputs"],
            psbt=funded["psbt"],
            status="created",
            metadata={
                "feeSats": funded["feeSats"],
                "changePosition": funded["changePosition"],
            },
            network=self._config.chain,
        )
        return {
            "recordId": record_id,
            "wallet": funded["wallet"],
            "psbt": funded["psbt"],
            "feeSats": funded["feeSats"],
            "totalOutputSats": funded["totalOutputSats"],
            "changePosition": funded["changePosition"],
            "complete": False,
        }

    def create_sign_and_send(
        self,
        wallet_name: str,
        body: dict[str, Any],
        *,
        idempotency_key: str | None,
        request_hash: str,
        claim_token: str | None,
    ) -> dict[str, Any]:
        """Prepare, finalize, and broadcast one transaction under an owned claim.

        The exact raw transaction is persisted before broadcast. On retries, the
        service checks whether it is already known before resubmitting; unknown
        broadcast outcomes remain reconcilable instead of creating a new spend.
        """
        if not self._config.allow_broadcast:
            raise ApiError(
                403,
                "BROADCAST_DISABLED",
                "Broadcasting is disabled. Set BITCOIN_BACKEND_ALLOW_BROADCAST=true to enable it.",
            )
        if not idempotency_key or not claim_token:
            raise ApiError(428, "IDEMPOTENCY_REQUIRED", "Idempotency-Key is required for broadcast requests.")
        if body.get("confirmBroadcast") is not True:
            raise ApiError(400, "BROADCAST_CONFIRMATION_REQUIRED", "confirmBroadcast must be true.")

        prepared = self._store.load_prepared_broadcast(idempotency_key, request_hash)
        if prepared is None:
            funded = self._fund_psbt(wallet_name, body, lock_unspents=True)
            try:
                processed = self._rpc.call(
                    "walletprocesspsbt",
                    [funded["psbt"], True, "ALL", False],
                    wallet=funded["wallet"],
                )
                finalized = self._rpc.call("finalizepsbt", [processed.get("psbt"), True])
                if not finalized.get("complete") or not finalized.get("hex"):
                    raise ApiError(409, "PSBT_NOT_FINAL", "Bitcoin Core could not finalize the PSBT.")
                raw_tx = str(finalized["hex"])
                decoded = self._rpc.call("decoderawtransaction", [raw_tx])
                expected_txid = validate_txid(decoded.get("txid") if isinstance(decoded, dict) else None)
                prepared = self._store.save_prepared_broadcast(
                    key=idempotency_key,
                    claim_token=claim_token,
                    request_hash=request_hash,
                    wallet=funded["wallet"],
                    outputs=funded["outputs"],
                    psbt=str(processed.get("psbt") or funded["psbt"]),
                    raw_tx=raw_tx,
                    expected_txid=expected_txid,
                    fee_sats=funded["feeSats"],
                    total_output_sats=funded["totalOutputSats"],
                    network=self._config.chain,
                )
            except Exception:
                self._unlock_psbt_inputs(funded["wallet"], funded["psbt"])
                raise

        self._require_claim_heartbeat(idempotency_key, claim_token)
        if self._transaction_known(prepared.wallet, prepared.expected_txid):
            self._store.mark_broadcast(idempotency_key, claim_token, prepared.expected_txid)
            return self._broadcast_result(prepared)

        mempool = self._rpc.call("testmempoolaccept", [[prepared.raw_tx]])
        first = mempool[0] if mempool else {}
        if not first.get("allowed"):
            if self._transaction_known(prepared.wallet, prepared.expected_txid):
                self._store.mark_broadcast(idempotency_key, claim_token, prepared.expected_txid)
                return self._broadcast_result(prepared)
            self._store.mark_prepared_failed(
                idempotency_key,
                claim_token,
                409,
                "MEMPOOL_REJECTED",
                "Bitcoin Core rejected the prepared transaction.",
            )
            self._unlock_psbt_inputs(prepared.wallet, prepared.psbt)
            raise ApiError(
                409,
                "MEMPOOL_REJECTED",
                "Bitcoin Core rejected the transaction.",
                {"rejectReason": first.get("reject-reason")},
            )
        try:
            returned_txid = validate_txid(self._rpc.call("sendrawtransaction", [prepared.raw_tx]))
        except RpcError as error:
            if self._transaction_known(prepared.wallet, prepared.expected_txid):
                returned_txid = prepared.expected_txid
            else:
                self._store.mark_unknown(
                    idempotency_key,
                    claim_token,
                    prepared.expected_txid,
                    str(error),
                )
                raise ApiError(
                    503 if error.status_code < 500 else error.status_code,
                    "BROADCAST_RESULT_UNKNOWN",
                    "Bitcoin broadcast result is unknown; the exact prepared transaction will be reconciled before retry.",
                    {"txid": prepared.expected_txid},
                ) from error
        if returned_txid != prepared.expected_txid:
            self._store.mark_unknown(
                idempotency_key,
                claim_token,
                prepared.expected_txid,
                "Bitcoin Core returned a different txid",
            )
            raise ApiError(
                502,
                "BROADCAST_TXID_MISMATCH",
                "Bitcoin Core returned a txid different from the persisted transaction.",
            )
        self._store.mark_broadcast(idempotency_key, claim_token, returned_txid)
        return self._broadcast_result(prepared)

    def wallet_transaction(self, wallet_name: str, txid_value: str) -> dict[str, Any]:
        """Return one wallet transaction with monetary values represented in satoshis."""
        wallet = validate_wallet_name(wallet_name)
        txid = validate_txid(txid_value)
        tx = self._rpc.call("gettransaction", [txid, True, True], wallet=wallet)
        return {
            "wallet": wallet,
            "txid": tx.get("txid", txid),
            "amountSats": btc_to_sats(tx.get("amount", 0)),
            "feeSats": btc_to_sats(tx.get("fee", 0)) if tx.get("fee") is not None else None,
            "confirmations": tx.get("confirmations", 0),
            "trusted": tx.get("trusted"),
            "details": tx.get("details", []),
            "blockhash": tx.get("blockhash"),
            "time": tx.get("time"),
            "bip125Replaceable": tx.get("bip125-replaceable"),
        }

    def cohesion_status(self, wallet_name: str | None = None) -> dict[str, Any]:
        """Report RPC, wallet-load, chain, persistent-store, and recent-transaction state."""
        wallet = validate_wallet_name(wallet_name or self._config.default_wallet)
        chain = self._rpc.call("getblockchaininfo")
        loaded_wallets = self._rpc.call("listwallets")
        wallet_loaded = wallet in loaded_wallets
        store_summary = self._store.summary()
        recent = self._store.recent_transactions(wallet if wallet_loaded else None, limit=10)
        return {
            "wallet": wallet,
            "checks": {
                "rpcReachable": True,
                "walletLoaded": wallet_loaded,
                "chainKnown": bool(chain.get("chain")),
                "initialBlockDownload": bool(chain.get("initialblockdownload")),
                "localStateWritable": True,
            },
            "chain": {
                "name": chain.get("chain"),
                "blocks": chain.get("blocks"),
                "headers": chain.get("headers"),
                "verificationProgress": chain.get("verificationprogress"),
            },
            "state": store_summary,
            "recentTransactions": recent,
        }

    def _fund_psbt(
        self,
        wallet_name: str,
        body: dict[str, Any],
        *,
        lock_unspents: bool,
    ) -> dict[str, Any]:
        """Validate outputs and ask Core to fund an unsigned PSBT.

        ``lock_unspents`` reserves selected inputs when preparing a broadcast;
        plain PSBT creation leaves input-lock policy to the supplied options.
        The returned mapping contains normalized outputs and fee/change metadata.
        """
        wallet = validate_wallet_name(wallet_name)
        outputs = normalize_outputs(body, self._config.max_outputs_per_tx, self._config.max_send_sats)
        self._validate_outputs_on_core(outputs)
        options = self._funding_options(body, lock_unspents=lock_unspents)
        core_outputs = [
            {item["address"]: sats_to_btc_string(item["amountSats"])}
            for item in outputs
        ]
        result = self._rpc.call(
            "walletcreatefundedpsbt",
            [[], core_outputs, 0, options, True],
            wallet=wallet,
        )
        psbt = result.get("psbt") if isinstance(result, dict) else None
        if not psbt:
            raise ApiError(502, "PSBT_MISSING", "Bitcoin Core did not return a funded PSBT.")
        return {
            "wallet": wallet,
            "outputs": outputs,
            "psbt": str(psbt),
            "feeSats": btc_to_sats(result.get("fee", 0)),
            "totalOutputSats": sum(item["amountSats"] for item in outputs),
            "changePosition": result.get("changepos"),
        }

    def _transaction_known(self, wallet: str, txid: str) -> bool:
        """Check mempool first and then the wallet for an already known txid."""
        try:
            self._rpc.call("getmempoolentry", [txid])
            return True
        except RpcError:
            pass
        try:
            self._rpc.call("gettransaction", [txid, True, True], wallet=wallet)
            return True
        except RpcError:
            return False

    def _require_claim_heartbeat(self, key: str, claim_token: str) -> None:
        """Refresh the idempotency lease or stop before broadcasting without ownership."""
        if not self._store.heartbeat_claim(key, claim_token):
            raise ApiError(
                409,
                "IDEMPOTENCY_CLAIM_LOST",
                "This worker no longer owns the broadcast claim.",
            )

    @staticmethod
    def _broadcast_result(prepared) -> dict[str, Any]:
        """Build the stable response shape from a persisted prepared broadcast."""
        return {
            "recordId": prepared.record_id,
            "wallet": prepared.wallet,
            "txid": prepared.expected_txid,
            "feeSats": prepared.fee_sats,
            "totalOutputSats": prepared.total_output_sats,
            "mempoolAccepted": True,
        }

    def _unlock_psbt_inputs(self, wallet: str, psbt: str) -> None:
        """Best-effort unlock inputs referenced by a PSBT after preparation failure."""
        try:
            decoded = self._rpc.call("decodepsbt", [psbt])
            vin = decoded.path("tx").path("vin") if hasattr(decoded, "path") else None
            if vin is None and isinstance(decoded, dict):
                vin = (decoded.get("tx") or {}).get("vin")
            if not isinstance(vin, list):
                return
            inputs = [
                {"txid": item.get("txid"), "vout": item.get("vout")}
                for item in vin
                if isinstance(item, dict)
                and item.get("txid")
                and isinstance(item.get("vout"), int)
            ]
            if inputs:
                self._rpc.call("lockunspent", [True, inputs], wallet=wallet)
        except Exception:
            # Best effort only. The persisted prepared transaction remains the source of truth.
            return

    def _validate_outputs_on_core(self, outputs: list[dict[str, Any]]) -> None:
        """Ask Bitcoin Core to validate every destination before funding a PSBT."""
        for item in outputs:
            address = validate_address_literal(item["address"])
            result = self._rpc.call("validateaddress", [address])
            if not result.get("isvalid"):
                raise ApiError(400, "INVALID_BITCOIN_ADDRESS", "Bitcoin Core rejected an output address.")

    def _funding_options(self, body: dict[str, Any], *, lock_unspents: bool) -> dict[str, Any]:
        """Translate API fee/replaceability options to walletcreatefundedpsbt keys."""
        conf_target = parse_non_negative_int(body.get("confTarget"), "confTarget", 6, 1008)
        if conf_target == 0:
            conf_target = 6
        estimate_mode = str(body.get("estimateMode") or "economical").upper()
        if estimate_mode not in {"UNSET", "ECONOMICAL", "CONSERVATIVE"}:
            raise ApiError(400, "INVALID_ESTIMATE_MODE", "estimateMode must be economical or conservative.")

        subtract = body.get("subtractFeeFromOutputs", [])
        if subtract is None:
            subtract = []
        if not isinstance(subtract, list) or any(not isinstance(index, int) for index in subtract):
            raise ApiError(400, "INVALID_SUBTRACT_FEE", "subtractFeeFromOutputs must be a list of output indexes.")

        return {
            "conf_target": conf_target,
            "estimate_mode": estimate_mode,
            "replaceable": bool(body.get("replaceable", True)),
            "lockUnspents": lock_unspents or bool(body.get("lockUnspents", False)),
            "subtractFeeFromOutputs": subtract,
            "change_type": str(body.get("changeType") or "bech32"),
        }


def fingerprint_for_request(method: str, path: str, body: dict[str, Any] | None) -> str:
    """Create the canonical idempotency fingerprint for an HTTP method/path/body."""
    return request_fingerprint(method, path, body or {})
