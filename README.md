# jwt-notify

A small web service that signs requests to [GOV.UK Notify](https://www.notifications.service.gov.uk/).

Notify authenticates with a JWT that expires 30 seconds after it is issued,
which a client holding a single static credential — an Azure Logic App custom
connector, say — cannot produce. This service sits in between and does it:

* the **`/v2/…` proxy routes** mirror the Notify API, mint a fresh JWT per call
  and forward the request. Callers authenticate to *this* service with a static
  secret, and never see a Notify API key.
* **`POST /token`** returns a bare token instead, for callers that want to make
  the onward call themselves.

```
Logic App ──static secret──▶ jwt-notify ──fresh JWT──▶ GOV.UK Notify
```

## Proxy API

The proxy paths and payloads are Notify's own, so
[Notify's REST API documentation](https://docs.notifications.service.gov.uk/rest-api.html)
describes them accurately. Responses are passed through untouched, including
Notify's validation errors and rate-limit responses.

| Operation | Route |
| --------- | ----- |
| `SendSms` | `POST /v2/notifications/sms` |
| `SendEmail` | `POST /v2/notifications/email` |
| `SendLetter` | `POST /v2/notifications/letter` |
| `SendPrecompiledLetter` | `POST /v2/notifications/letter/precompiled` |
| `GetNotification` | `GET /v2/notifications/{notification_id}` |
| `GetNotifications` | `GET /v2/notifications` |
| `GetLetterPdf` | `GET /v2/notifications/{notification_id}/pdf` |
| `GetReceivedTextMessages` | `GET /v2/received-text-messages` |
| `GetTemplates` | `GET /v2/templates` |
| `GetTemplate` | `GET /v2/template/{template_id}` |
| `GetTemplateVersion` | `GET /v2/template/{template_id}/version/{version}` |
| `PreviewTemplate` | `POST /v2/template/{template_id}/preview` |

One path differs from Notify. Notify accepts both templated and precompiled
letters on `/v2/notifications/letter`, distinguished by the body it receives.
Swagger 2.0 cannot express that choice, so precompiled letters get their own
route here and each connector operation ends up with a single schema. Both
forward to `/v2/notifications/letter` upstream.

Delivery receipts and inbound messages still reach you the usual way: Notify
posts callbacks straight to whatever URL you configure in the Notify admin
portal, which does not involve this service. `GetReceivedTextMessages` covers
the polling alternative.

```bash
curl -X POST https://notify-proxy.example.com/v2/notifications/sms \
  -H 'Content-Type: application/json' \
  -H 'X-Proxy-Key: <your secret>' \
  -d '{"phone_number":"+447900900123","template_id":"f33517ff-...","personalisation":{"name":"Amala"}}'
```

## Configuration

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `NOTIFY_API_KEY` | — | The Notify API key to sign with. Without it the `/v2` routes return `503` and only `/token` works. |
| `PROXY_KEY` | — | Shared secret callers must send as `X-Proxy-Key`. Unset means no check. |
| `NOTIFY_BASE_URL` | `https://api.notifications.service.gov.uk` | Useful for pointing at a stub in tests. |
| `NOTIFY_TIMEOUT_SECONDS` | `30` | Upstream timeout. Exceeding it returns `504`. |

A malformed `NOTIFY_API_KEY` fails at startup rather than on the first send.

### Authentication

Two layers, and you want at least one of them:

* **`PROXY_KEY`** — a shared secret in the `X-Proxy-Key` header, compared in
  constant time. This is what the generated connector definition declares as
  its API key.
* **A front end that authenticates for you**, such as Cloudflare Access with a
  service token. Leave `PROXY_KEY` unset and the service trusts its caller; it
  logs a warning at startup so this is never silent.

Anyone who can reach the proxy can send messages as your Notify service, so do
not leave it both open and publicly reachable.

## Azure Logic App custom connector

Custom connectors import **Swagger 2.0** only, while FastAPI emits OpenAPI 3.1,
so `connector/swagger.json` is generated:

```bash
python scripts/build_connector_swagger.py --host notify-proxy.example.com
```

Import that file in the Azure portal under **Logic Apps custom connector →
Create → Import an OpenAPI file**. Set the connector's security to **API Key**,
header name `X-Proxy-Key`, and supply the secret when creating the connection.
If Cloudflare Access is doing the authenticating instead, use its service-token
headers here.

`POST /token` is left out of the connector by default — handing it the ability
to mint bare tokens invites exactly the pattern the proxy exists to remove. Pass
`--all` if you want it anyway.

The generator refuses to translate schema constructs it does not understand
rather than emitting a connector that misbehaves in the designer, and
`tests/test_connector_swagger.py` checks the committed file matches the routes.

## Token API

### `POST /token`

Request:

```json
{ "api_key": "my_test_key-26785a09-ab16-4eb0-8407-a37497a57506-3d844edf-8d35-48ac-975b-e847b4f122b0" }
```

Response `200`:

```json
{
  "token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiIyNjc4NWEwOS1hYjE2LTRlYjAtODQwNy1hMzc0OTdhNTc1MDYiLCJpYXQiOjE1Njg4MTg1Nzh9...",
  "key_name": "my_test_key",
  "iss": "26785a09-ab16-4eb0-8407-a37497a57506",
  "iat": 1568818578,
  "expires_in": 30
}
```

Use it against Notify as `Authorization: Bearer <token>`. Notify rejects a token
whose `iat` is more than 30 seconds old, so request one per call rather than
caching it.

Errors:

| Status | Cause |
| ------ | ----- |
| `400`  | `api_key` is present but not in the Notify key format |
| `422`  | `api_key` is missing, empty, or the body is not JSON |
| `405`  | Wrong method (e.g. `GET /token`) |

The secret key is never echoed in a response or an error message.

### `GET /health`

Returns `{"status": "ok", "proxy_enabled": true}`. `proxy_enabled` is false when
no `NOTIFY_API_KEY` is configured. It needs no `X-Proxy-Key`, so it works as a
container health check.

Interactive docs are served at `/docs`.

### Proxy error responses

| Status | Cause |
| ------ | ----- |
| `401`  | `X-Proxy-Key` missing or wrong, when `PROXY_KEY` is set |
| `422`  | The request failed validation here and was never forwarded |
| `502`  | Notify could not be reached |
| `503`  | No `NOTIFY_API_KEY` is configured |
| `504`  | Notify did not answer within `NOTIFY_TIMEOUT_SECONDS` |

Anything else is Notify's own response, forwarded unchanged — including `400`
validation errors and `429` rate limits, whose `Retry-After` header is
preserved.

## Token format

The generated token matches the Notify specification exactly.

Header:

```json
{ "typ": "JWT", "alg": "HS256" }
```

Payload:

```json
{ "iss": "26785a09-ab16-4eb0-8407-a37497a57506", "iat": 1568818578 }
```

`iat` is the current UTC time in epoch seconds. The signature is
HMAC-SHA256 over `base64url(header).base64url(payload)`, keyed with the secret
key taken from the API key.

## API key format

A Notify API key is `{key_name}-{iss-uuid}-{secret-key-uuid}`. For
`my_test_key-26785a09-ab16-4eb0-8407-a37497a57506-3d844edf-8d35-48ac-975b-e847b4f122b0`:

* key name: `my_test_key`
* `iss` (service id): `26785a09-ab16-4eb0-8407-a37497a57506`
* secret key: `3d844edf-8d35-48ac-975b-e847b4f122b0`

Because the key name may itself contain hyphens, the two UUIDs are read from the
end of the string, not by splitting on `-` from the left.

## Running it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --port 8000
```

```bash
curl -X POST localhost:8000/token \
  -H 'Content-Type: application/json' \
  -d '{"api_key":"my_test_key-26785a09-ab16-4eb0-8407-a37497a57506-3d844edf-8d35-48ac-975b-e847b4f122b0"}'
```

With Docker:

```bash
docker build -t jwt-notify .
docker run -p 8000:8000 \
  -e NOTIFY_API_KEY='my_key-26785a09-...-3d844edf-...' \
  -e PROXY_KEY='a long random secret' \
  jwt-notify
```

## Deploying to a Proxmox LXC

`deploy/proxmox-lxc.sh` creates an unprivileged Debian 12 container, installs the
service into it and runs it under systemd. Run it **on the Proxmox host, as
root**:

```bash
./deploy/proxmox-lxc.sh
```

It picks the next free VMID, downloads the Debian template if the host does not
already have one, and takes DHCP by default. Anything can be overridden:

```bash
VMID=142 MEMORY=1024 IPV4=192.168.1.50/24 GATEWAY=192.168.1.1 ./deploy/proxmox-lxc.sh
```

If the repository is private, export a token with read access first
(`GITHUB_TOKEN=... ./deploy/proxmox-lxc.sh`); it is passed to the container
through the environment rather than the command line, and is stripped from the
clone's remote URL afterwards.

The script finishes by checking `/health` and generating a token from the
documented example key, then prints the container's address. Afterwards:

```bash
pct exec <vmid> -- journalctl -u jwt-notify -f       # logs
pct exec <vmid> -- systemctl restart jwt-notify      # restart
```

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The suite checks key parsing, the exact header and payload shape, and verifies
generated tokens against PyJWT as an independent implementation. PyJWT is a test
dependency only — the service itself signs tokens with the standard library.

## Deployment notes

The service handles live API keys, so it should only be reachable over TLS and
from trusted callers. It is stateless and keeps nothing on disk: keys are used to
sign a token and then discarded, and nothing about a request is logged beyond
uvicorn's default access line (method, path and status — no body).
