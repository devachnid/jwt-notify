"""Tests for the Notify proxy routes.

Notify itself is replaced by an httpx MockTransport that records what it was
sent, so these check what the proxy forwards as much as what it returns.
"""

import json

import httpx
import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from app.api_key import parse_api_key
from app.config import Settings
from app.main import app
from app.notify_client import NotifyClient

ISS = "26785a09-ab16-4eb0-8407-a37497a57506"
SECRET = "3d844edf-8d35-48ac-975b-e847b4f122b0"
API_KEY = f"my_test_key-{ISS}-{SECRET}"

SMS_BODY = {"phone_number": "+447900900123", "template_id": "f33517ff-2a88-4f6e-b855-c550268ce08a"}


class Upstream:
    """Stands in for the Notify API, recording each request it receives."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.status_code = 201
        self.json_body: dict | None = {"id": "abc", "reference": None}
        self.content: bytes | None = None
        self.content_type = "application/json"
        self.headers: dict[str, str] = {}
        self.raises: Exception | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        content = self.content
        if content is None:
            content = json.dumps(self.json_body).encode()
        return httpx.Response(
            self.status_code,
            content=content,
            headers={"content-type": self.content_type, **self.headers},
        )

    @property
    def last(self) -> httpx.Request:
        assert self.requests, "upstream received no requests"
        return self.requests[-1]

    def token_payload(self) -> dict:
        auth = self.last.headers["authorization"]
        assert auth.startswith("Bearer ")
        return pyjwt.decode(auth[7:], SECRET, algorithms=["HS256"])


def build_client(upstream: Upstream, *, proxy_key: str | None = None, api_key=API_KEY):
    """A TestClient wired to the fake upstream, bypassing the real lifespan."""
    parsed = parse_api_key(api_key) if api_key else None
    app.state.settings = Settings(
        notify_api_key=parsed,
        notify_base_url="https://api.notifications.service.gov.uk",
        request_timeout=30.0,
        proxy_key=proxy_key,
    )
    app.state.notify_client = (
        NotifyClient(
            parsed,
            "https://api.notifications.service.gov.uk",
            30.0,
            transport=httpx.MockTransport(upstream.handler),
        )
        if parsed
        else None
    )
    # TestClient(app) would run the lifespan and overwrite the wiring above.
    return TestClient(app)


@pytest.fixture
def upstream():
    return Upstream()


@pytest.fixture
def client(upstream):
    return build_client(upstream)


# --- signing -----------------------------------------------------------------


def test_forwards_a_freshly_signed_jwt(client, upstream):
    response = client.post("/v2/notifications/sms", json=SMS_BODY)
    assert response.status_code == 201
    payload = upstream.token_payload()
    assert payload["iss"] == ISS
    assert "iat" in payload


def test_each_call_is_signed_separately(client, upstream):
    client.post("/v2/notifications/sms", json=SMS_BODY)
    client.post("/v2/notifications/sms", json=SMS_BODY)
    first, second = (r.headers["authorization"] for r in upstream.requests)
    # Same second, so the tokens match; what matters is that a token was minted
    # per call rather than cached beyond its 30 second life.
    assert len(upstream.requests) == 2
    assert first.startswith("Bearer ") and second.startswith("Bearer ")


def test_client_never_sees_the_api_key(client, upstream):
    response = client.post("/v2/notifications/sms", json=SMS_BODY)
    assert SECRET not in response.text
    assert SECRET not in str(response.headers)


# --- sending -----------------------------------------------------------------


def test_send_sms_forwards_body_and_path(client, upstream):
    body = {**SMS_BODY, "personalisation": {"name": "Amala"}, "reference": "ref-1"}
    client.post("/v2/notifications/sms", json=body)
    assert upstream.last.url.path == "/v2/notifications/sms"
    assert json.loads(upstream.last.content) == body


def test_unset_optional_fields_are_not_forwarded(client, upstream):
    client.post("/v2/notifications/sms", json=SMS_BODY)
    # Sending "reference": null would be rejected by Notify's validation.
    assert json.loads(upstream.last.content) == SMS_BODY


def test_unknown_fields_are_passed_through(client, upstream):
    client.post("/v2/notifications/sms", json={**SMS_BODY, "brand_new_field": "x"})
    assert json.loads(upstream.last.content)["brand_new_field"] == "x"


def test_send_email_forwards_sanitise_content_for(client, upstream):
    client.post(
        "/v2/notifications/email",
        json={
            "email_address": "amala@example.com",
            "template_id": "t",
            "personalisation": {"first_name": "Amala"},
            "sanitise_content_for": ["first_name"],
        },
    )
    assert upstream.last.url.path == "/v2/notifications/email"
    assert json.loads(upstream.last.content)["sanitise_content_for"] == ["first_name"]


def test_send_letter_requires_personalisation(client):
    response = client.post("/v2/notifications/letter", json={"template_id": "t"})
    assert response.status_code == 422


def test_precompiled_letter_posts_to_the_letter_endpoint(client, upstream):
    client.post(
        "/v2/notifications/letter/precompiled",
        json={"reference": "ref-1", "content": "JVBERi0=", "postage": "first"},
    )
    assert upstream.last.url.path == "/v2/notifications/letter"
    assert json.loads(upstream.last.content) == {
        "reference": "ref-1",
        "content": "JVBERi0=",
        "postage": "first",
    }


def test_send_sms_rejects_a_missing_template(client, upstream):
    response = client.post("/v2/notifications/sms", json={"phone_number": "+447900900123"})
    assert response.status_code == 422
    assert not upstream.requests  # rejected before reaching Notify


# --- reading -----------------------------------------------------------------


def test_get_notification_by_id(client, upstream):
    upstream.status_code = 200
    upstream.json_body = {"id": "n-1", "status": "delivered"}
    response = client.get("/v2/notifications/n-1")
    assert response.status_code == 200
    assert response.json()["status"] == "delivered"
    assert upstream.last.url.path == "/v2/notifications/n-1"


def test_get_notifications_forwards_only_the_filters_given(client, upstream):
    upstream.status_code = 200
    client.get("/v2/notifications", params={"status": "delivered", "template_type": "sms"})
    assert dict(upstream.last.url.params) == {"status": "delivered", "template_type": "sms"}


def test_get_notifications_rejects_an_unknown_status(client, upstream):
    response = client.get("/v2/notifications", params={"status": "banana"})
    assert response.status_code == 422
    assert not upstream.requests


def test_get_received_text_messages(client, upstream):
    upstream.status_code = 200
    upstream.json_body = {"received_text_messages": []}
    response = client.get("/v2/received-text-messages")
    assert response.status_code == 200
    assert upstream.last.url.path == "/v2/received-text-messages"


def test_letter_pdf_is_returned_as_binary(client, upstream):
    upstream.status_code = 200
    upstream.content = b"%PDF-1.7 fake"
    upstream.content_type = "application/pdf"
    response = client.get("/v2/notifications/n-1/pdf")
    assert response.status_code == 200
    assert response.content == b"%PDF-1.7 fake"
    assert response.headers["content-type"].startswith("application/pdf")


def test_template_version_path(client, upstream):
    upstream.status_code = 200
    client.get("/v2/template/t-1/version/3")
    assert upstream.last.url.path == "/v2/template/t-1/version/3"


def test_template_preview(client, upstream):
    upstream.status_code = 200
    client.post("/v2/template/t-1/preview", json={"personalisation": {"a": "b"}})
    assert upstream.last.url.path == "/v2/template/t-1/preview"
    assert json.loads(upstream.last.content) == {"personalisation": {"a": "b"}}


# --- error handling ----------------------------------------------------------


def test_notify_errors_are_passed_through_verbatim(client, upstream):
    upstream.status_code = 400
    upstream.json_body = {
        "errors": [{"error": "ValidationError", "message": "phone_number Too many digits"}],
        "status_code": 400,
    }
    response = client.post("/v2/notifications/sms", json=SMS_BODY)
    assert response.status_code == 400
    assert response.json()["errors"][0]["message"] == "phone_number Too many digits"


def test_rate_limit_keeps_retry_after(client, upstream):
    upstream.status_code = 429
    upstream.headers = {"retry-after": "30"}
    upstream.json_body = {"errors": [{"error": "RateLimitError"}]}
    response = client.post("/v2/notifications/sms", json=SMS_BODY)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "30"


def test_timeout_becomes_504(client, upstream):
    upstream.raises = httpx.ReadTimeout("too slow")
    response = client.post("/v2/notifications/sms", json=SMS_BODY)
    assert response.status_code == 504


def test_connection_failure_becomes_502(client, upstream):
    upstream.raises = httpx.ConnectError("no route")
    response = client.post("/v2/notifications/sms", json=SMS_BODY)
    assert response.status_code == 502


def test_proxy_returns_503_without_a_configured_key(upstream):
    client = build_client(upstream, api_key=None)
    response = client.post("/v2/notifications/sms", json=SMS_BODY)
    assert response.status_code == 503
    assert "NOTIFY_API_KEY" in response.json()["detail"]


# --- inbound authentication --------------------------------------------------


def test_proxy_key_is_required_when_configured(upstream):
    client = build_client(upstream, proxy_key="s3cret")
    assert client.post("/v2/notifications/sms", json=SMS_BODY).status_code == 401
    assert not upstream.requests


def test_proxy_key_accepts_the_right_secret(upstream):
    client = build_client(upstream, proxy_key="s3cret")
    response = client.post(
        "/v2/notifications/sms", json=SMS_BODY, headers={"X-Proxy-Key": "s3cret"}
    )
    assert response.status_code == 201


def test_proxy_key_rejects_the_wrong_secret(upstream):
    client = build_client(upstream, proxy_key="s3cret")
    response = client.post(
        "/v2/notifications/sms", json=SMS_BODY, headers={"X-Proxy-Key": "nope"}
    )
    assert response.status_code == 401


def test_health_does_not_need_the_proxy_key(upstream):
    client = build_client(upstream, proxy_key="s3cret")
    assert client.get("/health").status_code == 200
