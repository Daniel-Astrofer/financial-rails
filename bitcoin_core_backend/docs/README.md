<!--
Kerosene documentation metadata
status: review-required
audience: internal
owner: rails
source_of_truth: rails
last_reviewed: 2026-09-03
-->

# Bitcoin Core Flask Backend

Este módulo expõe uma pequena API Flask em torno do JSON-RPC do Bitcoin Core para operações de carteira, criação de PSBT, broadcast opcional e coesão local de requisições.

## Padrões de segurança

- Autenticação por chave de API é obrigatória, a menos que `BITCOIN_BACKEND_AUTH_DISABLED=true` seja explicitamente definido para desenvolvimento local.
- Requisições de mutação devem usar `application/json`.
- Broadcast direto está desabilitado a menos que `BITCOIN_BACKEND_ALLOW_BROADCAST=true`.
- Criação de carteiras está desabilitada a menos que `BITCOIN_BACKEND_ALLOW_WALLET_CREATE=true`.
- `/transactions/send` exige um cabeçalho `Idempotency-Key` e `confirmBroadcast: true`.
- Valores são aceitos como satoshis inteiros e convertidos para strings em BTC antes das chamadas RPC do Bitcoin Core.
- Chaves privadas e frases-senha de carteiras não são aceitas por esta API.

## Ambiente

```sh
BITCOIN_RPC_URL=http://bitcoin-core:8332
BITCOIN_RPC_USER=kerosene
BITCOIN_RPC_PASSWORD=change-me
BITCOIN_RPC_WALLET=kerosene
BITCOIN_BACKEND_API_KEYS=use-a-long-random-token
BITCOIN_BACKEND_DB_PATH=/var/lib/kerosene/bitcoin-core-backend.sqlite3
```

## Executar

```sh
cd bitcoin_core_backend
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
gunicorn 'bitcoin_core_backend.app:create_app()' --bind 0.0.0.0:8090 --workers 2 --threads 8
# ou
gunicorn wsgi:app --bind 0.0.0.0:8090 --workers 2 --threads 8
```

## Endpoints

- `GET /healthz`
- `GET /v1/node/status`
- `POST /v1/wallets`
- `GET /v1/wallets/{wallet}/balance`
- `POST /v1/wallets/{wallet}/addresses`
- `GET /v1/wallets/{wallet}/utxos`
- `POST /v1/wallets/{wallet}/transactions/psbt`
- `POST /v1/wallets/{wallet}/transactions/send`
- `GET /v1/wallets/{wallet}/transactions/{txid}`
- `GET /v1/cohesion/status?wallet={wallet}`

Exemplo de requisição PSBT:

```json
{
  "outputs": [
    {"address": "bc1q...", "amountSats": 25000}
  ],
  "confTarget": 6,
  "estimateMode": "economical",
  "replaceable": true
}
```
