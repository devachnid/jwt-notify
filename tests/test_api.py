import time

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from app.main import app

ISS = "26785a09-ab16-4eb0-8407-a37497a57506"
SECRET = "3d844edf-8d35-48ac-975b-e847b4f122b0"
EXAMPLE_KEY = f"my_test_key-{ISS}-{SECRET}"


PROXY_KEY = "s3cret"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("NOTIFY_API_KEY", raising=False)
    monkeypatch.delenv("PROXY_AUTH", raising=False)
    monkeypatch.setenv("PROXY_KEY", PROXY_KEY)
    with TestClient(app, headers={"X-Proxy-Key": PROXY_KEY}) as test_client:
        yield test_client


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "proxy_enabled": False}


def test_returns_a_usable_token(client):
    before = int(time.time())
    response = client.post("/token", json={"api_key": EXAMPLE_KEY})
    assert response.status_code == 200

    body = response.json()
    assert body["key_name"] == "my_test_key"
    assert body["iss"] == ISS
    assert body["expires_in"] == 30
    assert before <= body["iat"] <= int(time.time())

    decoded = pyjwt.decode(body["token"], SECRET, algorithms=["HS256"])
    assert decoded == {"iss": ISS, "iat": body["iat"]}


def test_response_does_not_include_the_secret_key(client):
    response = client.post("/token", json={"api_key": EXAMPLE_KEY})
    assert SECRET not in response.text


@pytest.mark.parametrize("bad_key", ["not-a-key", f"my_test_key-{ISS}", ""])
def test_malformed_key_returns_400(client, bad_key):
    response = client.post("/token", json={"api_key": bad_key})
    assert response.status_code in (400, 422)
    assert "token" not in response.json()


def test_missing_api_key_returns_422_without_echoing_the_body(client):
    response = client.post("/token", json={"apikey": EXAMPLE_KEY})
    assert response.status_code == 422
    assert SECRET not in response.text


def test_non_json_body_returns_422(client):
    response = client.post(
        "/token", content="api_key=" + EXAMPLE_KEY,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 422
    assert SECRET not in response.text


def test_get_is_not_allowed(client):
    assert client.get("/token").status_code == 405


def test_token_requires_the_proxy_key(client):
    response = client.post(
        "/token", json={"api_key": EXAMPLE_KEY}, headers={"X-Proxy-Key": "wrong"}
    )
    assert response.status_code == 401
    assert "token" not in response.json()


def test_health_needs_no_proxy_key(client):
    assert client.get("/health", headers={"X-Proxy-Key": ""}).status_code == 200


def test_docs_are_not_served_by_default(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_oversized_body_is_refused(client):
    response = client.post(
        "/token",
        content=b'{"api_key": "' + b"x" * (6 * 1024 * 1024) + b'"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_oversized_chunked_body_is_refused(client):
    def chunks():
        for _ in range(7):
            yield b"x" * (1024 * 1024)

    response = client.post(
        "/token", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413


def test_body_under_the_limit_still_reaches_the_route(client):
    # Replaying the buffered body must leave it intact for the route.
    response = client.post("/token", json={"api_key": EXAMPLE_KEY, "pad": "x" * 100_000})
    assert response.status_code == 200
