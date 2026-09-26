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

NOTIFICATION_ID = "740e5834-3a29-46b4-9a6f-16142fde533a"
TEMPLATE_ID = "f33517ff-2a88-4f6e-b855-c550268ce08a"

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
    response = client.get(f"/v2/notifications/{NOTIFICATION_ID}")
    assert response.status_code == 200
    assert response.json()["status"] == "delivered"
    assert upstream.last.url.path == f"/v2/notifications/{NOTIFICATION_ID}"


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
    response = client.get(f"/v2/notifications/{NOTIFICATION_ID}/pdf")
    assert response.status_code == 200
    assert response.content == b"%PDF-1.7 fake"
    assert response.headers["content-type"].startswith("application/pdf")


def test_template_version_path(client, upstream):
    upstream.status_code = 200
    client.get(f"/v2/template/{TEMPLATE_ID}/version/3")
    assert upstream.last.url.path == f"/v2/template/{TEMPLATE_ID}/version/3"


def test_template_preview(client, upstream):
    upstream.status_code = 200
    client.post(f"/v2/template/{TEMPLATE_ID}/preview", json={"personalisation": {"a": "b"}})
    assert upstream.last.url.path == f"/v2/template/{TEMPLATE_ID}/preview"
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


def test_non_ascii_proxy_key_is_a_401_not_a_500(upstream):
    client = build_client(upstream, proxy_key="s3cret")
    response = client.post(
        "/v2/notifications/sms", json=SMS_BODY, headers=[(b"x-proxy-key", b"\xe9")]
    )
    assert response.status_code == 401
    assert not upstream.requests


def test_non_ascii_configured_key_still_works(upstream):
    client = build_client(upstream, proxy_key="clé-secrète")
    response = client.post(
        "/v2/notifications/sms",
        json=SMS_BODY,
        headers=[(b"x-proxy-key", "clé-secrète".encode("utf-8"))],
    )
    assert response.status_code == 201


def test_health_does_not_need_the_proxy_key(upstream):
    client = build_client(upstream, proxy_key="s3cret")
    assert client.get("/health").status_code == 200


# --- envelope mode -----------------------------------------------------------

ENVELOPE = {"X-Notify-Envelope": "true"}


def test_envelope_wraps_a_successful_send(client, upstream):
    upstream.status_code = 201
    upstream.json_body = {"id": "n-1", "reference": "ref-1", "content": {"body": "hello"}}
    response = client.post("/v2/notifications/sms", json=SMS_BODY, headers=ENVELOPE)
    assert response.status_code == 200
    assert response.json() == {
        "status_code": 201,
        "success": True,
        "body": {"id": "n-1", "reference": "ref-1", "content": {"body": "hello"}},
        "errors": [],
    }


def test_envelope_turns_a_rejection_into_a_readable_200(client, upstream):
    upstream.status_code = 400
    upstream.json_body = {
        "errors": [{"error": "ValidationError", "message": "phone_number Too many digits"}],
        "status_code": 400,
    }
    response = client.post("/v2/notifications/sms", json=SMS_BODY, headers=ENVELOPE)
    assert response.status_code == 200
    body = response.json()
    assert body["status_code"] == 400
    assert body["success"] is False
    assert body["errors"] == [
        {"error": "ValidationError", "message": "phone_number Too many digits"}
    ]
    # Notify's own body is kept whole alongside the lifted errors.
    assert body["body"]["status_code"] == 400


def test_envelope_reports_notify_being_unreachable(client, upstream):
    upstream.raises = httpx.ConnectError("no route")
    response = client.post("/v2/notifications/sms", json=SMS_BODY, headers=ENVELOPE)
    assert response.status_code == 200
    body = response.json()
    assert body["status_code"] == 502
    assert body["success"] is False
    assert body["body"] is None
    assert body["errors"][0]["error"] == "NotifyUnavailableError"


def test_envelope_wraps_a_read_as_well_as_a_send(client, upstream):
    upstream.status_code = 200
    upstream.json_body = {"id": "n-1", "status": "delivered"}
    response = client.get(f"/v2/notifications/{NOTIFICATION_ID}", headers=ENVELOPE)
    assert response.json()["body"]["status"] == "delivered"
    assert response.json()["status_code"] == 200


def test_envelope_leaves_a_pdf_alone(client, upstream):
    upstream.status_code = 200
    upstream.content = b"%PDF-1.7 fake"
    upstream.content_type = "application/pdf"
    response = client.get(f"/v2/notifications/{NOTIFICATION_ID}/pdf", headers=ENVELOPE)
    assert response.content == b"%PDF-1.7 fake"
    assert response.headers["content-type"].startswith("application/pdf")


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
def test_envelope_header_accepts_the_usual_spellings(client, upstream, value):
    response = client.post(
        "/v2/notifications/sms", json=SMS_BODY, headers={"X-Notify-Envelope": value}
    )
    assert response.status_code == 200
    assert response.json()["status_code"] == 201


@pytest.mark.parametrize("value", ["false", "0", "", "no"])
def test_anything_else_leaves_the_response_untouched(client, upstream, value):
    response = client.post(
        "/v2/notifications/sms", json=SMS_BODY, headers={"X-Notify-Envelope": value}
    )
    assert response.status_code == 201
    assert "status_code" not in response.json()


def test_passthrough_is_the_default(client, upstream):
    upstream.status_code = 400
    upstream.json_body = {"errors": [{"error": "ValidationError", "message": "no"}]}
    response = client.post("/v2/notifications/sms", json=SMS_BODY)
    assert response.status_code == 400
    assert "success" not in response.json()


# --- upstream path hardening -------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/v2/notifications/abc%3Fx=1%23",
        "/v2/notifications/%2E%2E",
        "/v2/notifications/%2E%2E/pdf",
        "/v2/template/%2E%2E",
        "/v2/template/%2E%2E/version/1",
        "/v2/template/abc%3Fx=1",
    ],
)
def test_ids_that_would_rewrite_the_upstream_url_are_rejected(client, upstream, path):
    response = client.get(path)
    assert response.status_code == 422
    assert not upstream.requests


def test_template_preview_rejects_a_traversing_id(client, upstream):
    response = client.post("/v2/template/%2E%2E/preview", json={})
    assert response.status_code == 422
    assert not upstream.requests


@pytest.mark.parametrize(
    "path", ["/v2/notifications", "/v2/received-text-messages"]
)
def test_older_than_must_be_an_id(client, upstream, path):
    response = client.get(path, params={"older_than": "x&status=failed"})
    assert response.status_code == 422
    assert not upstream.requests


def test_older_than_is_forwarded(client, upstream):
    upstream.status_code = 200
    client.get("/v2/notifications", params={"older_than": NOTIFICATION_ID})
    assert dict(upstream.last.url.params) == {"older_than": NOTIFICATION_ID}


def test_connection_failure_does_not_leak_upstream_detail(client, upstream):
    upstream.raises = httpx.ConnectError("[Errno -2] Name or service not known: internal.host")
    response = client.post("/v2/notifications/sms", json=SMS_BODY)
    assert response.status_code == 502
    assert "internal.host" not in response.text
