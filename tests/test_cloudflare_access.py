"""Tests for verifying Cloudflare Access tokens at the origin.

Access signs a JWT for every request it lets through and sends it as
``Cf-Access-Jwt-Assertion``. These tests sign their own tokens with throwaway
RSA keys and serve the public halves from a fake certs endpoint, so they check
the verification itself rather than a stand-in for it.
"""

import asyncio
import json
import time

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.api_key import parse_api_key
from app.cloudflare_access import AccessDenied, AccessUnavailable, AccessVerifier
from app.config import CloudflareAccess, Settings
from app.main import app
from app.notify_client import NotifyClient

ISS = "26785a09-ab16-4eb0-8407-a37497a57506"
SECRET = "3d844edf-8d35-48ac-975b-e847b4f122b0"
API_KEY = f"my_test_key-{ISS}-{SECRET}"
SMS_BODY = {"phone_number": "+447900900123", "template_id": "f33517ff-2a88-4f6e-b855-c550268ce08a"}

AUD = "4714c1358e65fe4b408ad6d432a5f878f08194bdb4752441fd56faefa9b2b6f2"
ACCESS = CloudflareAccess(team_domain="myteam.cloudflareaccess.com", aud=AUD)
HEADER = "Cf-Access-Jwt-Assertion"


def _new_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


KEY = _new_key()
OTHER_KEY = _new_key()


class Certs:
    """Stands in for https://<team>.cloudflareaccess.com/cdn-cgi/access/certs."""

    def __init__(self, keys: dict[str, rsa.RSAPrivateKey]):
        self.keys = dict(keys)
        self.fetches = 0
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.fetches += 1
        assert str(request.url) == ACCESS.certs_url
        if self.fail:
            return httpx.Response(500)
        jwks = []
        for kid, key in self.keys.items():
            jwk = json.loads(pyjwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
            jwks.append({**jwk, "kid": kid, "alg": "RS256", "use": "sig"})
        return httpx.Response(200, json={"keys": jwks})


def mint(key=KEY, kid="k1", alg="RS256", **overrides) -> str:
    now = int(time.time())
    claims = {
        "aud": [AUD],
        "iss": ACCESS.issuer,
        "iat": now,
        "nbf": now,
        "exp": now + 300,
        "sub": "",
        "type": "app",
        "common_name": "abc123.access",
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return pyjwt.encode(claims, key, algorithm=alg, headers={"kid": kid} if kid else None)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def certs():
    return Certs({"k1": KEY})


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def verifier(certs, clock):
    return AccessVerifier(
        ACCESS, timeout=5.0, transport=httpx.MockTransport(certs.handler), clock=clock
    )


def verify(verifier, token):
    asyncio.run(verifier.verify(token))


# --- the verifier ------------------------------------------------------------


def test_accepts_a_token_signed_by_access(verifier):
    verify(verifier, mint())


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(lambda: mint(aud=["someone-elses-app"]), id="wrong-audience"),
        pytest.param(lambda: mint(aud=None), id="no-audience"),
        pytest.param(lambda: mint(iss="https://evil.cloudflareaccess.com"), id="wrong-issuer"),
        pytest.param(lambda: mint(exp=int(time.time()) - 3600), id="expired"),
        pytest.param(lambda: mint(exp=None), id="no-expiry"),
        pytest.param(lambda: mint(nbf=int(time.time()) + 3600), id="not-yet-valid"),
        pytest.param(lambda: mint(key=OTHER_KEY), id="signed-by-another-key"),
        pytest.param(lambda: mint(kid="unknown"), id="unknown-key-id"),
        pytest.param(lambda: mint(kid=None), id="no-key-id"),
        pytest.param(lambda: mint(key=SECRET, alg="HS256"), id="hs256"),
        pytest.param(lambda: mint(key=None, alg="none"), id="alg-none"),
        pytest.param(lambda: "not.a.jwt", id="garbage"),
        pytest.param(lambda: "", id="empty"),
    ],
)
def test_rejects_tokens_access_did_not_issue_for_this_app(verifier, token):
    with pytest.raises(AccessDenied):
        verify(verifier, token())


def test_public_keys_are_cached(verifier, certs):
    verify(verifier, mint())
    verify(verifier, mint())
    assert certs.fetches == 1


def test_a_new_key_is_picked_up_after_rotation(verifier, certs, clock):
    verify(verifier, mint())
    certs.keys["k2"] = OTHER_KEY
    clock.now += 61
    verify(verifier, mint(key=OTHER_KEY, kid="k2"))
    assert certs.fetches == 2


def test_unknown_key_ids_cannot_force_repeated_fetches(verifier, certs):
    verify(verifier, mint())
    for _ in range(5):
        with pytest.raises(AccessDenied):
            verify(verifier, mint(kid=f"made-up-{_}"))
    assert certs.fetches == 1


def test_keys_are_refreshed_once_they_are_old(verifier, certs, clock):
    verify(verifier, mint())
    clock.now += 2 * 3600
    verify(verifier, mint())
    assert certs.fetches == 2


def test_unreachable_certs_with_no_cached_keys_is_unavailable(verifier, certs):
    certs.fail = True
    with pytest.raises(AccessUnavailable):
        verify(verifier, mint())


def test_cached_keys_survive_a_failed_refresh(verifier, certs, clock):
    verify(verifier, mint())
    certs.fail = True
    clock.now += 2 * 3600
    verify(verifier, mint())


# --- the service in cloudflare-access mode -----------------------------------


class Upstream:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(201, json={"id": "abc"})


@pytest.fixture
def upstream():
    return Upstream()


@pytest.fixture
def client(upstream, verifier):
    parsed = parse_api_key(API_KEY)
    app.state.settings = Settings(
        notify_api_key=parsed,
        notify_base_url="https://api.notifications.service.gov.uk",
        request_timeout=30.0,
        proxy_key=None,
        cloudflare_access=ACCESS,
    )
    app.state.notify_client = NotifyClient(
        parsed,
        "https://api.notifications.service.gov.uk",
        30.0,
        transport=httpx.MockTransport(upstream.handler),
    )
    app.state.access_verifier = verifier
    # TestClient(app) without a context manager skips the lifespan, which
    # would replace the wiring above.
    return TestClient(app)


def test_a_request_that_passed_access_is_forwarded(client, upstream):
    response = client.post("/v2/notifications/sms", json=SMS_BODY, headers={HEADER: mint()})
    assert response.status_code == 201
    assert len(upstream.requests) == 1


def test_a_request_without_the_access_token_is_refused(client, upstream):
    response = client.post("/v2/notifications/sms", json=SMS_BODY)
    assert response.status_code == 401
    assert not upstream.requests


def test_a_forged_access_token_is_refused(client, upstream):
    response = client.post(
        "/v2/notifications/sms", json=SMS_BODY, headers={HEADER: mint(key=OTHER_KEY)}
    )
    assert response.status_code == 401
    assert not upstream.requests


def test_a_proxy_key_is_no_substitute_for_the_access_token(client, upstream):
    response = client.post(
        "/v2/notifications/sms", json=SMS_BODY, headers={"X-Proxy-Key": "anything"}
    )
    assert response.status_code == 401


def test_token_endpoint_needs_the_access_token_too(client):
    assert client.post("/token", json={"api_key": API_KEY}).status_code == 401
    response = client.post("/token", json={"api_key": API_KEY}, headers={HEADER: mint()})
    assert response.status_code == 200


def test_health_needs_no_access_token(client):
    assert client.get("/health").status_code == 200


def test_unreachable_certs_is_a_503_not_an_open_door(client, upstream, certs):
    certs.fail = True
    response = client.post("/v2/notifications/sms", json=SMS_BODY, headers={HEADER: mint()})
    assert response.status_code == 503
    assert not upstream.requests
