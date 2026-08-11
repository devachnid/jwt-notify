# jwt-notify

A small web service that turns a [GOV.UK Notify](https://www.notifications.service.gov.uk/)
API key into the short-lived JWT that the Notify API expects as a bearer token.

## API

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

Returns `{"status": "ok"}`.

Interactive docs are served at `/docs`.

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
docker run -p 8000:8000 jwt-notify
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
