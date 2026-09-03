<!--
Kerosene documentation metadata
status: review-required
audience: internal
owner: rails
source_of_truth: rails
last_reviewed: 2026-09-03
-->

# Guia de Implantação do Lightning Flask

Este guia descreve como executar o backend Lightning Flask como um serviço interno conectado a um nó LND REST.

## Topologia de Implantação

Topologia recomendada:

```text
clientes internos
  -> proxy reverso privado ou service mesh
  -> lightning_flask em 127.0.0.1 ou interface privada
  -> endpoint LND REST
  -> estado da carteira/canais LND
```

Mantenha o serviço Flask e o endpoint LND REST em uma rede privada. Não publique o LND REST, macaroons ou esta fachada diretamente na internet pública.

## Pré-requisitos de Execução

- Python 3.10+.
- `lightning_flask/requirements.txt` instalado em um ambiente virtual.
- Um token `KEROSENE_API_TOKEN` longo e aleatório.
- LND REST acessível a partir do host ou contêiner.
- Um macaroon com as permissões necessárias para:
  - `GET /v1/getinfo`
  - `GET /v1/balance/blockchain`
  - `GET /v1/balance/channels`
  - `GET /v1/channels`
  - `POST /v1/invoices`
  - `GET /v1/invoice/{payment_hash}`
  - `POST /v1/channels/transactions`
  - `GET /v1/payments`
- Certificado CA TLS do LND quando o LND usar um certificado autoassinado ou privado.
- Um diretório gravável persistente para `LIGHTNING_BACKEND_SQLITE`.

## Arquivo de Ambiente

Crie um arquivo de ambiente legível apenas pela conta de serviço:

```bash
sudo install -d -m 0750 -o kerosene -g kerosene /etc/kerosene
sudoedit /etc/kerosene/lightning-flask.env
sudo chmod 0640 /etc/kerosene/lightning-flask.env
sudo chown root:kerosene /etc/kerosene/lightning-flask.env
```

Exemplo:

```dotenv
KEROSENE_API_TOKEN=replace-with-at-least-32-random-characters
LIGHTNING_LND_REST_URL=https://127.0.0.1:8080
LIGHTNING_LND_MACAROON_PATH=/var/lib/lnd/data/chain/bitcoin/mainnet/admin.macaroon
LIGHTNING_LND_TLS_CERT_PATH=/var/lib/lnd/tls.cert
LIGHTNING_LND_TIMEOUT_SECONDS=8
LIGHTNING_BACKEND_SQLITE=/var/lib/kerosene/lightning/lightning_backend.sqlite3
LIGHTNING_BACKEND_RATE_LIMIT_PER_MINUTE=120
LIGHTNING_BACKEND_MAX_INVOICE_SATS=50000000
LIGHTNING_DEFAULT_INVOICE_EXPIRY_SECONDS=3600
```

Prefira `LIGHTNING_LND_MACAROON_PATH` em vez de `LIGHTNING_LND_MACAROON_HEX` para hosts de longa duração, para que o segredo não seja incorporado diretamente em dumps de configuração de processo. Em plataformas de contêiner, monte o macaroon como um segredo ou volume somente leitura.

## Instalar em um Host

```bash
sudo useradd --system --home /var/lib/kerosene --shell /usr/sbin/nologin kerosene || true
sudo install -d -m 0750 -o kerosene -g kerosene /opt/kerosene
sudo install -d -m 0750 -o kerosene -g kerosene /var/lib/kerosene/lightning

cd /opt/kerosene
sudo git clone /path/or/url/to/Kerosene repo
cd kerosene-rails/lightning_flask
sudo -u kerosene python -m venv .venv
sudo -u kerosene .venv/bin/pip install -r requirements.txt
```

Para um repositório já presente no host, use o caminho real do checkout em vez de clonar.

## Executar com Flask para Desenvolvimento

```bash
cd lightning_flask
set -a
. /etc/kerosene/lightning-flask.env
set +a
flask --app app:create_app run --host 127.0.0.1 --port 8091
```

O servidor de desenvolvimento do Flask é aceitável apenas para desenvolvimento local.

## Executar com Gunicorn

`gunicorn` não está listado em `requirements.txt`, portanto instale-o no ambiente de execução se você usar este modelo de processo:

```bash
cd lightning_flask
. .venv/bin/activate
pip install gunicorn
gunicorn 'app:create_app()' \
  --bind 127.0.0.1:8091 \
  --workers 2 \
  --threads 4 \
  --timeout 30 \
  --access-logfile - \
  --error-logfile -
```

Use uma contagem baixa de workers, a menos que a contenção de gravação do SQLite tenha sido testada sob o tráfego esperado. O aplicativo usa o modo WAL do SQLite e gravações curtas, mas registros de idempotência e eventos de coesão são armazenamento de processo local, não um sistema de coordenação distribuído.

## Unit Systemd

Arquivo de unit de exemplo em `/etc/systemd/system/kerosene-lightning-flask.service`:

```ini
[Unit]
Description=Kerosene Lightning Flask backend
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=kerosene
Group=kerosene
WorkingDirectory=/opt/kerosene/kerosene-rails/lightning_flask
EnvironmentFile=/etc/kerosene/lightning-flask.env
ExecStart=/opt/kerosene/kerosene-rails/lightning_flask/.venv/bin/flask --app app:create_app run --host 127.0.0.1 --port 8091
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/kerosene/lightning

[Install]
WantedBy=multi-user.target
```

Para produção, substitua `ExecStart` por um servidor WSGI como o Gunicorn após instalá-lo:

```ini
ExecStart=/opt/kerosene/kerosene-rails/lightning_flask/.venv/bin/gunicorn app:create_app() --bind 127.0.0.1:8091 --workers 2 --threads 4 --timeout 30
```

Em seguida, habilite o serviço:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kerosene-lightning-flask
sudo systemctl status kerosene-lightning-flask
```

## Proxy Reverso

Exemplo de location Nginx para um gateway interno:

```nginx
location /lightning/ {
    proxy_pass http://127.0.0.1:8091/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 64k;
}
```

Certifique-se de que o proxy não registre `Authorization`, `Idempotency-Key`, faturas BOLT11, macaroons ou corpos de resposta.

## Notas sobre Contêineres

Não há um Dockerfile dedicado para `lightning_flask` neste repositório. Se você o empacotar como um contêiner:

- Use o diretório `lightning_flask` como diretório de trabalho do aplicativo.
- Instale `requirements.txt` mais um servidor WSGI, se necessário.
- Monte o macaroon e o certificado TLS do LND como somente leitura.
- Monte um volume gravável persistente para o SQLite.
- Vincule o serviço a uma interface privada dentro da rede de implantação.

Formato mínimo de imagem:

```Dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn
COPY . .
USER 10001:10001
EXPOSE 8091
CMD ["gunicorn", "app:create_app()", "--bind", "0.0.0.0:8091", "--workers", "2", "--threads", "4"]
```

Compile a partir de `lightning_flask` se usar esse exemplo.

## Integração com LND

O aplicativo chama o LND REST com:

```http
Grpc-Metadata-macaroon: <hex macaroon>
Content-Type: application/json
Accept: application/json
```

Quando `LIGHTNING_LND_TLS_CERT_PATH` está definido, o Python usa esse certificado como arquivo CA para verificação HTTPS. O host de `LIGHTNING_LND_REST_URL` deve corresponder à identidade do certificado LND ou a um domínio/IP TLS adicional configurado.

Para a pilha compose local do Kerosene, os dados do LND são armazenados no volume `lnd_data`. Os serviços maiores do aplicativo montam esse volume como somente leitura e usam caminhos semelhantes a:

```text
/lnd/tls.cert
/lnd/data/chain/bitcoin/${BITCOIN_NETWORK}/admin.macaroon
```

Espelhe esse padrão se você adicionar esta fachada Flask à topologia compose.

## Persistência e Backups

O SQLite armazena:

- `idempotency`: chave da requisição, fingerprint, resposta em cache, código de status, hora de criação.
- `lightning_events`: tipo de evento, hash de pagamento, valor, status, metadados sanitizados, hora de criação.

Faça backup do arquivo SQLite se você precisar de repetição durável de retentativas ou histórico operacional de eventos. O banco de dados não armazena preimages de pagamento, macaroons, tokens de portador ou faturas BOLT11 nos metadados de eventos, mas respostas de idempotência em cache podem incluir payloads normais de resposta da API.

## Verificações de Saúde

Saúde do processo:

```bash
curl -fsS http://127.0.0.1:8091/health
```

Verificação de caminho LND autenticado:

```bash
curl -fsS http://127.0.0.1:8091/v1/node/status \
  -H "Authorization: Bearer $KEROSENE_API_TOKEN"
```

`/health` apenas confirma que o Flask está respondendo. Use `/v1/node/status` para verificar a acessibilidade do LND, validade do macaroon e configuração de TLS.

## Lista de Verificação Operacional

- Rotacione `KEROSENE_API_TOKEN` através do gerenciador de segredos da implantação.
- Mantenha as permissões do macaroon tão restritas quanto prático para os endpoints suportados.
- Armazene o banco de dados SQLite em armazenamento local persistente.
- Mantenha o serviço em uma rede privada e coloque a aplicação de autenticação na frente de todas as rotas não relacionadas à saúde.
- Alerte sobre respostas repetidas de `401`, `429`, `502` e `503`.
- Monitore os campos de sincronização do LND a partir de `/v1/node/status`.
- Valide o backup e a restauração do arquivo SQLite se o histórico de retentativas idempotentes for importante.
- Execute a suíte de testes antes das implantações.

## Solução de Problemas

| Sintoma | Causa provável | Ação |
| --- | --- | --- |
| O aplicativo falha na inicialização com `KEROSENE_API_TOKEN must be at least 32 characters` | Token de API ausente ou curto | Gere um token aleatório mais longo e reinicie. |
| O aplicativo falha na inicialização com erro de macaroon | Nenhuma fonte de macaroon ou caminho ilegível | Defina `LIGHTNING_LND_MACAROON_HEX` ou corrija as permissões de `LIGHTNING_LND_MACAROON_PATH`. |
| `401 unauthorized` | Token de portador ausente/inválido | Envie `Authorization: Bearer <token>` correspondente a `KEROSENE_API_TOKEN`. |
| `415 unsupported_media_type` | Requisição de mutação sem tipo de conteúdo JSON | Envie `Content-Type: application/json`. |
| `409 idempotency_conflict` | Mesmo `Idempotency-Key` reutilizado para requisição diferente | Gere uma nova chave para cada mutação lógica. |
| `429 rate_limited` | Taxa de requisição excedida | Reduza a taxa ou ajuste `LIGHTNING_BACKEND_RATE_LIMIT_PER_MINUTE`. |
| `502 lnd_http_error` | LND rejeitou a requisição proxyzada | Verifique os logs do LND, permissões do macaroon, estado da carteira e valores da requisição. |
| `503 lnd_unavailable` | URL do LND REST inacessível ou falha de TLS | Verifique `LIGHTNING_LND_REST_URL`, caminho do certificado, rede e status do processo LND. |

## Resumo Conciso

Implante o Lightning Flask como uma fachada privada autenticada na frente do LND REST. Forneça um token de API longo, URL do LND, macaroon, certificado TLS e caminho persistente do SQLite; execute-o atrás de um proxy reverso ou service mesh; use `/health` para verificações de processo e `/v1/node/status` para verificações de ponta a ponta do LND.
