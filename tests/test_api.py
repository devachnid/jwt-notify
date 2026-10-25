import asyncio
import time
from dataclasses import replace

import jwt as pyjwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import require_authenticated
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


# --- authentication happens before the body is read --------------------------


def _call(scope_headers, path="/token", receive=None):
    """Drive the ASGI app directly, so a test can see what it reads."""
    reads = []
    sent = []

    async def default_receive():
        reads.append(1)
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": scope_headers,
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "app": app,
    }
    # Bounded, so a missing read deadline fails the test instead of hanging it.
    asyncio.run(asyncio.wait_for(app(scope, receive or default_receive, send), 10))
    status_code = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status_code, reads


def test_an_unauthenticated_body_is_never_read(client):
    status_code, reads = _call(
        [(b"content-type", b"application/json"), (b"content-length", b"5000000")]
    )
    assert status_code == 401
    assert reads == []


def test_malformed_json_without_the_key_is_a_401_not_a_422(client):
    # The body must not even be parsed for a caller without the key.
    response = client.post(
        "/token",
        content=b"{not json",
        headers={"Content-Type": "application/json", "X-Proxy-Key": "wrong"},
    )
    assert response.status_code == 401


def test_unauthenticated_routes_only_accept_a_small_body(client):
    response = client.post(
        "/health", content=b"x" * 64 * 1024, headers={"X-Proxy-Key": ""}
    )
    assert response.status_code == 413


def test_a_stalled_body_is_cut_off(client, monkeypatch):
    monkeypatch.setattr(
        app.state, "settings", replace(app.state.settings, body_read_timeout=0.2)
    )
    first = True

    async def stall():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": b'{"api_key": "', "more_body": True}
        await asyncio.Event().wait()

    started = time.monotonic()
    status_code, _ = _call([(b"x-proxy-key", PROXY_KEY.encode())], receive=stall)
    assert status_code == 408
    assert time.monotonic() - started < 5


def test_a_route_reached_without_the_middleware_is_refused():
    # The route-level check is a backstop: if a protected route were ever
    # mounted where the middleware does not look, it fails closed.
    bare = FastAPI()

    @bare.get("/secret", dependencies=[Depends(require_authenticated)])
    async def secret():
        return {"ok": True}

    assert TestClient(bare).get("/secret").status_code == 401
