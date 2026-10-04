<!--
Kerosene documentation metadata
status: review-required
audience: internal
owner: rails
source_of_truth: rails
last_reviewed: 2026-09-03
-->

# Especificação da API Lightning Flask

Exemplos de URL base neste documento usam:

```text
http://127.0.0.1:8091
```

Todos os corpos de resposta são JSON. As rotas de negócio exigem autenticação via bearer token, as rotas administrativas usam `X-Kerosene-Admin-Key` e `GET /health` é público.

## Autenticação

Envie:

```http
Authorization: Bearer <KEROSENE_API_TOKEN>
```

Credenciais ausentes ou inválidas retornam:

```json
{
  "success": false,
  "error": {
    "code": "unauthorized",
    "message": "Missing bearer token"
  }
}
```

## Cabeçalhos Comuns

Cabeçalhos de requisição:

| Cabeçalho | Obrigatório | Descrição |
| --- | --- | --- |
| `Authorization` | Obrigatório nas rotas de negócio | Bearer token compartilhado ou token de leitura/escrita configurado. |
| `Content-Type: application/json` | Obrigatório para `POST`, `PUT`, `PATCH` | Requisições de mutação devem ser objetos JSON. |
| `Idempotency-Key` | Obrigatório para `POST /v1/invoices` e `POST /v1/payments` | Repete mutações lógicas idênticas e rejeita reuso conflitante. |

Cabeçalhos de resposta:

| Cabeçalho | Valor |
| --- | --- |
| `Cache-Control` | `no-store` |
| `X-Content-Type-Options` | `nosniff` |

## Envelope de Sucesso

Respostas bem-sucedidas incluem:

```json
{
  "success": true
}
```

Dados específicos do endpoint são retornados sob um campo nomeado como `node`, `invoice`, `payment`, `channels` ou `cohesion`.

## Envelope de Erro

Erros usam:

```json
{
  "success": false,
  "error": {
    "code": "invalid_amount",
    "message": "amount_sats must be positive"
  }
}
```

Códigos de erro comuns:

| Status HTTP | Código | Significado |
| --- | --- | --- |
| `400` | `bad_request` | Falha de validação genérica. |
| `400` | `invalid_json` | Corpo da requisição ausente ou não é um objeto JSON. |
| `400` | `invalid_amount` | Valor em satoshis ausente, inválido ou não positivo. |
| `400` | `amount_too_large` | Valor excede o limite configurado. |
| `400` | `invalid_integer` | Campo inteiro inválido ou fora da faixa aceita. |
| `400` | `invalid_memo` | Memo tem mais de 256 caracteres. |
| `400` | `invalid_invoice` | `payment_request` não é uma fatura BOLT11. |
| `400` | `invalid_payment_hash` | Hash de pagamento não tem 32 bytes codificados como 64 caracteres hexadecimais. |
| `401` | `unauthorized` | Bearer token ausente ou inválido. |
| `409` | `idempotency_conflict` | Chave de idempotência reutilizada com uma requisição diferente. |
| `409` | `idempotency_in_progress` | Outra requisição com a mesma chave ainda está em execução. |
| `413` | `payload_too_large` | Corpo da requisição excede `LIGHTNING_BACKEND_MAX_BODY_BYTES`. |
| `415` | `unsupported_media_type` | Requisição de mutação não usa o tipo de conteúdo JSON. |
| `429` | `rate_limited` | Limite de taxa excedido. |
| `500` | `internal_error` | Erro de aplicação não tratado. |
| `502` | `lnd_http_error` | LND retornou um erro HTTP. |
| `502` | `lnd_protocol_error` | LND retornou JSON inválido. |
| `503` | `lnd_unavailable` | Endpoint REST do LND indisponível. |

## Tipos de Dados e Validação

| Tipo | Regra |
| --- | --- |
| `payment_hash` | 64 caracteres hexadecimais, normalizados para minúsculas. |
| `amount_sats` | Inteiro positivo e não maior que `LIGHTNING_BACKEND_MAX_INVOICE_SATS` para criação de faturas. |
| `memo` | String de até 256 caracteres. Memo ausente torna-se string vazia. |
| `expiry_seconds` | Inteiro de `60` a `2592000`. Padrão é `LIGHTNING_DEFAULT_INVOICE_EXPIRY_SECONDS`. |
| `payment_request` | Fatura BOLT11 começando com `lnbc`, `lntb`, `lnbcrt` ou `lnbs`; 20 a 4096 caracteres de fatura após o prefixo. |
| `fee_limit_sats` | Inteiro de `1` a `1000000`, limitado também pela política de taxa máxima configurada. O padrão é calculado a partir do valor da fatura e de `LIGHTNING_BACKEND_MAX_FEE_PPM`. |
| `timeout_seconds` | Inteiro de `1` a `600`. Padrão é `60`. |

## `GET /health`

Verificação pública de saúde do processo.

### Requisição

```bash
curl -sS http://127.0.0.1:8091/health
```

### Resposta `200`

```json
{
  "success": true,
  "status": "ok"
}
```

## `GET /v1/node/status`

Retorna os campos operacionais públicos do nó LND. O endpoint administrativo `/v1/admin/node/status` mantém os detalhes completos; o cliente LND pode armazenar o status em cache por `LIGHTNING_BACKEND_STATUS_CACHE_SECONDS`.

### Requisição

```bash
curl -sS http://127.0.0.1:8091/v1/node/status \
  -H "Authorization: Bearer $KEROSENE_API_TOKEN"
```

### Resposta `200`

```json
{
  "success": true,
  "node": {
    "synced_to_chain": true,
    "synced_to_graph": true,
    "block_height": 848000,
    "network": "mainnet"
  }
}
```

## `GET /v1/channels`

Lista canais LND normalizados.

### Requisição

```bash
curl -sS http://127.0.0.1:8091/v1/channels \
  -H "Authorization: Bearer $KEROSENE_API_TOKEN"
```

### Resposta `200`

```json
{
  "success": true,
  "channels": [
    {
      "active": true,
      "remote_pubkey": "02bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "channel_point": "txid:0",
      "capacity_sats": 1000000,
      "local_balance_sats": 700000,
      "remote_balance_sats": 300000
    }
  ]
}
```

## `POST /v1/invoices`

Cria uma fatura Lightning privada através do LND.

### Corpo da Requisição

```json
{
  "amount_sats": 2500,
  "memo": "coffee",
  "expiry_seconds": 600
}
```

Campos:

| Campo | Obrigatório | Descrição |
| --- | --- | --- |
| `amount_sats` | Sim | Inteiro positivo limitado por `LIGHTNING_BACKEND_MAX_INVOICE_SATS`. |
| `memo` | Não | Memo de até 256 caracteres. Padrão é string vazia. |
| `expiry_seconds` | Não | Expiração da fatura de 60 segundos a 30 dias. Padrão é o valor configurado. |

### Exemplo

```bash
curl -sS -X POST http://127.0.0.1:8091/v1/invoices \
  -H "Authorization: Bearer $KEROSENE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: invoice-2026-06-13-001" \
  -d '{"amount_sats":2500,"memo":"coffee","expiry_seconds":600}'
```

### Resposta `201`

```json
{
  "success": true,
  "invoice": {
    "payment_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "payment_request": "lnbc2500n1...",
    "add_index": "42",
    "amount_sats": 2500,
    "memo": "coffee",
    "expiry_seconds": 600
  }
}
```

Efeito colateral: anexa um evento de coesão `invoice_created` sanitizado contendo `payment_hash`, `amount_sats`, `status: open`, `memo` e `expiry_seconds`.

## `GET /v1/invoices/{payment_hash}`

Consulta uma fatura pelo hash de pagamento.

### Parâmetros de Caminho

| Parâmetro | Regra |
| --- | --- |
| `payment_hash` | 64 caracteres hexadecimais minúsculos ou maiúsculos. |

### Requisição

```bash
curl -sS http://127.0.0.1:8091/v1/invoices/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  -H "Authorization: Bearer $KEROSENE_API_TOKEN"
```

### Resposta `200`

```json
{
  "success": true,
  "invoice": {
    "payment_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "payment_request": "lnbc2500n1...",
    "memo": "coffee",
    "amount_sats": 2500,
    "amount_paid_sats": 0,
    "state": "OPEN",
    "settled": false,
    "creation_date": "1781320000",
    "settle_date": "0",
    "expiry": "600"
  }
}
```

## `POST /v1/payments`

Envia um pagamento BOLT11 através do LND.

### Corpo da Requisição

```json
{
  "payment_request": "lnbc1...",
  "fee_limit_sats": 50,
  "timeout_seconds": 60
}
```

Campos:

| Campo | Obrigatório | Descrição |
| --- | --- | --- |
| `payment_request` | Sim | Fatura BOLT11. |
| `fee_limit_sats` | Não | Limite fixo de taxa de 1 a 1.000.000 sats, limitado também pela política de taxa máxima configurada. O padrão é calculado a partir do valor da fatura e de `LIGHTNING_BACKEND_MAX_FEE_PPM`. |
| `timeout_seconds` | Não | Tempo limite de pagamento LND de 1 a 600 segundos. Padrão é 60. |

### Exemplo

```bash
curl -sS -X POST http://127.0.0.1:8091/v1/payments \
  -H "Authorization: Bearer $KEROSENE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: payment-2026-06-13-001" \
  -d '{"payment_request":"lnbc1...","fee_limit_sats":50,"timeout_seconds":60}'
```

### Resposta `202`

```json
{
  "success": true,
  "payment": {
    "payment_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "payment_preimage": "preimage-if-returned-by-lnd",
    "payment_route": {},
    "payment_error": null,
    "status": "submitted",
    "fee_limit_sats": 50
  }
}
```

Se o LND retornar um erro de pagamento, a resposta ainda pode ser `202` com:

```json
{
  "success": true,
  "payment": {
    "payment_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "payment_preimage": null,
    "payment_route": null,
    "payment_error": "payment failed",
    "status": "failed",
    "fee_limit_sats": 50
  }
}
```

Efeito colateral: anexa um evento de coesão `payment_submitted` sanitizado contendo `payment_hash`, `status`, `fee_limit_sats` e `timeout_seconds`. A fatura BOLT11 e a preimagem não são escritas nos metadados do evento.

## `GET /v1/payments/{payment_hash}`

Consulta um pagamento pelo hash de pagamento.

### Requisição

```bash
curl -sS http://127.0.0.1:8091/v1/payments/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  -H "Authorization: Bearer $KEROSENE_API_TOKEN"
```

### Resposta `200`

```json
{
  "success": true,
  "payment": {
    "payment_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "status": "SUCCEEDED",
    "value_sats": 2500,
    "fee_sats": 2,
    "creation_time_ns": "1781320000000000000"
  }
}
```

Quando o LND não possui pagamento correspondente:

```json
{
  "success": true,
  "payment": {
    "payment_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "status": "unknown"
  }
}
```

## `GET /v1/cohesion/snapshot`

Retorna o estado local de idempotência e eventos Lightning sanitizados.

### Requisição

```bash
curl -sS http://127.0.0.1:8091/v1/cohesion/snapshot \
  -H "Authorization: Bearer $KEROSENE_API_TOKEN"
```

### Resposta `200`

```json
{
  "success": true,
  "cohesion": {
    "idempotency_records": 2,
    "lightning_events": 2,
    "recent_events": [
      {
        "event_type": "payment_submitted",
        "payment_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "amount_sats": null,
        "status": "submitted",
        "metadata": {
          "fee_limit_sats": 50,
          "timeout_seconds": 60
        },
        "created_at": 1781320000
      },
      {
        "event_type": "invoice_created",
        "payment_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "amount_sats": 2500,
        "status": "open",
        "metadata": {
          "memo": "coffee",
          "expiry_seconds": 600
        },
        "created_at": 1781319900
      }
    ]
  }
}
```

`recent_events` é limitado aos 25 registros mais recentes ordenados do mais novo para o mais antigo.

## Exemplos de Idempotência

Primeira requisição:

```bash
curl -i -X POST http://127.0.0.1:8091/v1/invoices \
  -H "Authorization: Bearer $KEROSENE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: invoice-repeatable" \
  -d '{"amount_sats":1000}'
```

Repetição com método, caminho e corpo idênticos retorna a mesma resposta e status.

Repetição com corpo diferente:

```bash
curl -i -X POST http://127.0.0.1:8091/v1/invoices \
  -H "Authorization: Bearer $KEROSENE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: invoice-repeatable" \
  -d '{"amount_sats":2000}'
```

Resposta:

```json
{
  "success": false,
  "error": {
    "code": "idempotency_conflict",
    "message": "Idempotency-Key was reused with a different request"
  }
}
```

## Resumo Conciso

Use `GET /health` sem autenticação para verificações de processo. Envie autenticação bearer para todas as rotas `/v1/*`, corpos JSON para rotas `POST` e chaves de idempotência para mutações de faturas/pagamentos repetíveis. Respostas usam um envelope consistente `{success, ...}`, com falhas de validação e erro do LND retornadas como JSON estruturado `{success:false,error:{code,message}}`.
