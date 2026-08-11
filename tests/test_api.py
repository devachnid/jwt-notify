import time

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from app.main import app

ISS = "26785a09-ab16-4eb0-8407-a37497a57506"
SECRET = "3d844edf-8d35-48ac-975b-e847b4f122b0"
EXAMPLE_KEY = f"my_test_key-{ISS}-{SECRET}"


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
