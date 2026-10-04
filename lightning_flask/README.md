<!--
Kerosene documentation metadata
status: review-required
audience: internal
owner: rails
source_of_truth: rails
last_reviewed: 2026-09-03
-->

# Lightning Flask Backend

O Lightning Flask backend é um pequeno serviço Python que expõe uma fachada HTTP protegida sobre um nó LND REST. Ele é destinado a serviços internos do Kerosene que precisam de status do nó Lightning, visibilidade de canais, criação de faturas, envio de pagamentos, consulta de pagamentos e um snapshot local de coesão sem expor o LND diretamente.

Este serviço:

- Exige autenticação via bearer-token para as rotas de negócio; rotas administrativas usam `X-Kerosene-Admin-Key` e `GET /health` é público.
- Faz proxy de um conjunto limitado de operações LND REST através de modelos de requisição validados.
- Exige chaves de idempotência para criação de faturas e envio de pagamentos.
- Armazena registros de idempotência e eventos operacionais sanitizados em SQLite.
- Evita persistir faturas BOLT11, macaroons, tokens de API ou preimages de pagamento no registro de eventos de coesão.

## Diretório

```text
lightning_flask/
  src/application/    Segurança, validação e contratos da capacidade Lightning
  src/adapters/       Flask inbound e adapters outbound para LND e SQLite
  src/config/         Settings baseados em variáveis de ambiente
  requirements.txt    Dependências de runtime Python
  tests/test_app.py   Testes de rotas e validação Flask com um cliente LND fake
  DEPLOYMENT.md       Guia de implantação e operação
  API_SPEC.md         Contrato da API HTTP
```

O código de transporte e integração fica em `src/adapters`; a capacidade não
deve importar Flask, SQLite ou o cliente HTTP do LND diretamente. A composição
da aplicação é `src.adapters.inbound.http.app:create_app`.

## Requisitos

- Python 3.10 ou superior.
- Flask 3.x.
- Um endpoint LND REST acessível.
- Um macaroon LND fornecido como string hexadecimal ou arquivo legível.
- O certificado TLS LND ao conectar a um endpoint HTTPS com certificado customizado/auto-assinado.

Instale a dependência Python:

```bash
cd lightning_flask
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Configuração

A aplicação lê configurações de variáveis de ambiente por padrão.

| Variável | Obrigatório | Padrão | Descrição |
| --- | --- | --- | --- |
| `KEROSENE_API_TOKEN` | Condicional | None | Token Bearer compartilhado de fallback quando `LIGHTNING_READ_TOKEN` e `LIGHTNING_WRITE_TOKEN` não estão definidos. Deve ter pelo menos 32 bytes de entropia. |
| `LIGHTNING_READ_TOKEN` | Condicional | None | Token Bearer para rotas de leitura. Deve ter pelo menos 32 bytes de entropia. |
| `LIGHTNING_WRITE_TOKEN` | Condicional | None | Token Bearer para rotas de mutação. Deve ter pelo menos 32 bytes de entropia. |
| `LIGHTNING_ADMIN_TOKEN` | Não | None | Chave para rotas administrativas. Deve ter pelo menos 32 bytes de entropia quando definida. |
| `LIGHTNING_LND_REST_URL` | Sim | `https://127.0.0.1:8080` | URL absoluta `http://` ou `https://` para LND REST. Não deve incluir credenciais, query ou fragmento. |
| `LIGHTNING_LND_MACAROON_HEX` | Uma fonte de macaroon necessária | None | Macaroon LND codificado em hexadecimal. |
| `LIGHTNING_LND_MACAROON_PATH` | Uma fonte de macaroon necessária | None | Caminho para um arquivo de macaroon LND. Usado quando `LIGHTNING_LND_MACAROON_HEX` está vazio. |
| `LIGHTNING_LND_TLS_CERT_PATH` | Geralmente para LND HTTPS | None | Caminho do certificado CA usado para verificar TLS do LND. |
| `LIGHTNING_LND_TIMEOUT_SECONDS` | Não | `8` | Timeout para chamadas REST LND. |
| `LIGHTNING_BACKEND_SQLITE` | Não | `/var/lib/kerosene/lightning-backend/state.sqlite3` | Caminho do banco de dados SQLite para idempotência e eventos de coesão. |
| `LIGHTNING_BACKEND_MAX_BODY_BYTES` | Não | `65536` | Tamanho máximo aceito do corpo da requisição. |
| `LIGHTNING_BACKEND_RATE_LIMIT_PER_MINUTE` | Não | `120` | Limite de requisições por token/IP em memória. |
| `LIGHTNING_BACKEND_STATUS_CACHE_SECONDS` | Não | `2` | Duração do cache para agregação de status do nó. |
| `LIGHTNING_BACKEND_MAX_INVOICE_SATS` | Não | `50000000` | `amount_sats` máximo aceito para criação de fatura. |
| `LIGHTNING_BACKEND_MAX_PAYMENT_SATS` | Não | `50000000` | Valor máximo aceito para pagamentos. |
| `LIGHTNING_DEFAULT_INVOICE_EXPIRY_SECONDS` | Não | `3600` | Expiração padrão de fatura quando omitida. |
| `HOST` | Não | `127.0.0.1` | Host usado pelo entrypoint local `python -m src.adapters.inbound.http.app`. |
| `PORT` | Não | `8091` | Porta usada pelo entrypoint local `python -m src.adapters.inbound.http.app`. |

## Execução Local

Inicie o serviço contra um endpoint LND REST local:

```bash
cd lightning_flask
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

export KEROSENE_API_TOKEN="$(python - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"
export LIGHTNING_LND_REST_URL="https://127.0.0.1:8080"
export LIGHTNING_LND_MACAROON_PATH="/path/to/admin.macaroon"
export LIGHTNING_LND_TLS_CERT_PATH="/path/to/tls.cert"
export LIGHTNING_BACKEND_SQLITE="/tmp/lightning_backend.sqlite3"

flask --app src.adapters.inbound.http.app:create_app run --host 127.0.0.1 --port 8091
```

Health é público:

```bash
curl http://127.0.0.1:8091/health
```

Requisições autenticadas exigem:

```http
Authorization: Bearer $KEROSENE_API_TOKEN
```

Exemplo de requisição de status do nó:

```bash
curl -sS http://127.0.0.1:8091/v1/node/status \
  -H "Authorization: Bearer $KEROSENE_API_TOKEN"
```

Exemplo de criação de fatura:

```bash
curl -sS -X POST http://127.0.0.1:8091/v1/invoices \
  -H "Authorization: Bearer $KEROSENE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: invoice-demo-001" \
  -d '{"amount_sats":2500,"memo":"coffee","expiry_seconds":600}'
```

Exemplo de envio de pagamento:

```bash
curl -sS -X POST http://127.0.0.1:8091/v1/payments \
  -H "Authorization: Bearer $KEROSENE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: payment-demo-001" \
  -d '{"payment_request":"lnbc1...","fee_limit_sats":50,"timeout_seconds":60}'
```

## Visão Geral dos Endpoints

| Método | Caminho | Autenticação | Propósito |
| --- | --- | --- | --- |
| `GET` | `/health` | Não | Verificação de saúde do processo. |
| `GET` | `/v1/node/status` | Sim | Status agregado do nó LND, carteira e saldo de canais. |
| `GET` | `/v1/channels` | Sim | Resumo de canais ativos e inativos. |
| `POST` | `/v1/invoices` | Sim | Criar uma fatura Lightning privada. |
| `GET` | `/v1/invoices/{payment_hash}` | Sim | Consultar uma fatura pelo hash de pagamento. |
| `POST` | `/v1/payments` | Sim | Enviar um pagamento BOLT11 para LND. |
| `GET` | `/v1/payments/{payment_hash}` | Sim | Consultar estado do pagamento pelo hash de pagamento. |
| `GET` | `/v1/cohesion/snapshot` | Sim | Retornar contagens de idempotência e eventos Lightning sanitizados recentes. |

Consulte [API_SPEC.md](API_SPEC.md) para o contrato completo de requisição e resposta.

## Idempotência

`POST /v1/invoices` e `POST /v1/payments` exigem um cabeçalho `Idempotency-Key`.

- Reutilizar a mesma chave com o mesmo método, caminho e corpo retorna a resposta original.
- Reutilizar a mesma chave com um corpo ou caminho diferente retorna `409 idempotency_conflict`.
- Uma requisição duplicada concorrente pode retornar `409 idempotency_in_progress`.
- Entradas de idempotência são armazenadas em SQLite e não expiram automaticamente por este serviço.

Use chaves de idempotência para tentativas de cliente em criação de faturas e envio de pagamentos.

## Modelo de Segurança

- LND nunca é exposto diretamente aos chamadores.
- O acesso do cliente é controlado por um bearer token compartilhado.
- O acesso LND usa `Grpc-Metadata-macaroon` e verificação opcional de TLS CA.
- Requisições de mutação devem usar `Content-Type: application/json`.
- Respostas incluem `Cache-Control: no-store` e `X-Content-Type-Options: nosniff`.
- A limitação de taxa de requisições é em memória e chaveada pelos últimos 8 caracteres do bearer token mais o endereço remoto.
- Metadados de eventos de coesão são sanitizados antes do armazenamento.

Este serviço é adequado para ser usado atrás de um limite de rede privada, gateway interno, proxy reverso ou service mesh. Não foi projetado para ser exposto diretamente à internet pública.

## Testes

Execute os testes unitários:

```bash
cd lightning_flask
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-test.txt
python -m pytest tests
```

Os testes usam um cliente LND fake e não requerem um nó LND ativo.

## Mais Documentação

- [DEPLOYMENT.md](DEPLOYMENT.md): modelo de processo, configuração de ambiente, proxy reverso, systemd, Docker, checklist de operações.
- [API_SPEC.md](API_SPEC.md): contrato de endpoint, regras de validação, envelope de erro, exemplos cURL.

## Resumo Conciso

Lightning Flask é uma fachada Flask autenticada para ações selecionadas do LND REST. Configure-a com um token de API, URL LND REST, macaroon, certificado TLS opcional e caminho SQLite; execute-a com Flask ou um servidor WSGI; use autenticação bearer mais chaves de idempotência para requisições de mutação de faturas e pagamentos.
