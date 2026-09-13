"""Checks on the Swagger 2.0 definition used by the Azure custom connector."""

import json
from pathlib import Path

import pytest
from openapi_spec_validator import validate
from openapi_spec_validator.validation.exceptions import OpenAPIValidationError

from scripts.build_connector_swagger import EXCLUDED_BY_DEFAULT, build

COMMITTED = Path(__file__).resolve().parent.parent / "connector" / "swagger.json"

EXPECTED_OPERATIONS = {
    "GetHealth",
    "SendSms",
    "SendEmail",
    "SendLetter",
    "SendPrecompiledLetter",
    "GetNotification",
    "GetNotifications",
    "GetLetterPdf",
    "GetReceivedTextMessages",
    "GetTemplates",
    "GetTemplate",
    "GetTemplateVersion",
    "PreviewTemplate",
}


@pytest.fixture(scope="module")
def document():
    return build("notify-proxy.example.com")


def operation_ids(doc) -> set[str]:
    return {op["operationId"] for ops in doc["paths"].values() for op in ops.values()}


def test_document_is_valid_swagger_2(document):
    try:
        validate(document)
    except OpenAPIValidationError as exc:
        pytest.fail(f"generated connector definition is not valid Swagger 2.0: {exc}")


def test_declares_swagger_2_not_openapi_3(document):
    # Azure custom connectors reject OpenAPI 3.
    assert document["swagger"] == "2.0"
    assert "openapi" not in document


def test_every_proxy_operation_is_present(document):
    assert operation_ids(document) == EXPECTED_OPERATIONS


def test_token_endpoint_is_excluded_by_default(document):
    assert "CreateToken" in EXCLUDED_BY_DEFAULT
    assert "CreateToken" not in operation_ids(document)
    assert "/token" not in document["paths"]


def test_all_flag_includes_the_token_endpoint():
    assert "CreateToken" in operation_ids(build("example.com", include_all=True))


def test_no_openapi_3_references_survive(document):
    assert "#/components/schemas" not in json.dumps(document)


def test_definitions_are_reachable(document):
    """Every $ref points at a definition that exists."""
    text = json.dumps(document)
    refs = {
        part.split('"')[0]
        for part in text.split('"$ref": "#/definitions/')[1:]
    }
    assert refs, "expected the body schemas to be referenced"
    assert refs <= set(document["definitions"])


def test_shared_secret_is_declared_as_the_security_scheme(document):
    scheme = document["securityDefinitions"]["proxyKey"]
    assert scheme["type"] == "apiKey"
    assert scheme["in"] == "header"
    assert scheme["name"] == "X-Proxy-Key"
    assert document["security"] == [{"proxyKey": []}]


def test_send_operations_take_a_body(document):
    for path in ("/v2/notifications/sms", "/v2/notifications/email"):
        params = document["paths"][path]["post"]["parameters"]
        body = [p for p in params if p["in"] == "body"]
        assert len(body) == 1
        assert body[0]["required"] is True


def test_letter_pdf_is_declared_as_binary(document):
    operation = document["paths"]["/v2/notifications/{notification_id}/pdf"]["get"]
    assert operation["produces"] == ["application/pdf"]
    assert operation["responses"]["200"]["schema"] == {"type": "string", "format": "binary"}


def test_query_parameters_are_flattened(document):
    """Swagger 2.0 puts type on the parameter, not in a nested schema."""
    params = document["paths"]["/v2/notifications"]["get"]["parameters"]
    by_name = {p["name"]: p for p in params}
    assert "schema" not in by_name["status"]
    assert by_name["status"]["type"] == "string"
    assert "delivered" in by_name["status"]["enum"]
    assert by_name["include_jobs"]["type"] == "boolean"
    assert by_name["status"]["required"] is False


def test_path_parameters_are_required(document):
    params = document["paths"]["/v2/notifications/{notification_id}"]["get"]["parameters"]
    assert [p["required"] for p in params if p["name"] == "notification_id"] == [True]


# --- Cloudflare Access variant ----------------------------------------------


@pytest.fixture(scope="module")
def cloudflare_document():
    return build("notify.example.com", cf_access_client_id="abc123.access")


def test_cloudflare_variant_is_valid(cloudflare_document):
    validate(cloudflare_document)


def test_cloudflare_secret_becomes_the_connection_credential(cloudflare_document):
    scheme = cloudflare_document["securityDefinitions"]["cfAccessClientSecret"]
    assert scheme["name"] == "CF-Access-Client-Secret"
    assert scheme["type"] == "apiKey"
    assert scheme["in"] == "header"
    assert cloudflare_document["security"] == [{"cfAccessClientSecret": []}]
    assert "proxyKey" not in cloudflare_document["securityDefinitions"]


def test_cloudflare_client_id_rides_on_every_operation(cloudflare_document):
    """The connector can only bind one header to its API key, so the id is fixed."""
    for path, operations in cloudflare_document["paths"].items():
        for method, operation in operations.items():
            headers = {
                p["name"]: p for p in operation["parameters"] if p["in"] == "header"
            }
            assert "CF-Access-Client-Id" in headers, f"missing on {method} {path}"
            header = headers["CF-Access-Client-Id"]
            assert header["default"] == "abc123.access"
            assert header["required"] is True
            # Hidden in the designer so it is sent without anyone editing it.
            assert header["x-ms-visibility"] == "internal"


def test_operations_are_the_same_in_both_variants(document, cloudflare_document):
    assert operation_ids(cloudflare_document) == operation_ids(document)


def test_no_cloudflare_headers_without_the_flag(document):
    headers = [
        p["name"]
        for ops in document["paths"].values()
        for op in ops.values()
        for p in op["parameters"]
        if p["in"] == "header"
    ]
    assert headers == []


# --- responses ---------------------------------------------------------------


def test_send_operations_declare_notifys_own_status_codes(document):
    """201 and 400, as the Notify documentation says — not FastAPI's defaults."""
    for path in (
        "/v2/notifications/sms",
        "/v2/notifications/email",
        "/v2/notifications/letter",
        "/v2/notifications/letter/precompiled",
    ):
        responses = document["paths"][path]["post"]["responses"]
        assert "201" in responses, path
        assert "200" not in responses, path
        for code in ("400", "403", "429"):
            assert responses[code]["schema"] == {"$ref": "#/definitions/NotifyError"}, path


def test_send_sms_success_carries_the_fields_a_logic_app_needs(document):
    """An empty schema gives the designer nothing to offer as dynamic content."""
    schema = document["paths"]["/v2/notifications/sms"]["post"]["responses"]["201"]["schema"]
    assert schema == {"$ref": "#/definitions/SmsResponse"}
    properties = document["definitions"]["SmsResponse"]["properties"]
    assert {"id", "reference", "uri", "template", "content"} <= set(properties)
    assert properties["content"]["$ref"] == "#/definitions/SmsContent"


def test_error_schema_describes_the_detail_notify_returns(document):
    error = document["definitions"]["NotifyError"]
    assert set(error["required"]) == {"status_code", "errors"}
    assert error["properties"]["errors"]["items"]["$ref"] == "#/definitions/NotifyErrorDetail"
    detail = document["definitions"]["NotifyErrorDetail"]["properties"]
    assert set(detail) == {"error", "message"}


def test_read_operations_describe_their_bodies(document):
    expected = {
        ("/v2/notifications/{notification_id}", "get"): "NotificationResponse",
        ("/v2/notifications", "get"): "NotificationListResponse",
        ("/v2/received-text-messages", "get"): "ReceivedTextMessageListResponse",
        ("/v2/templates", "get"): "TemplateListResponse",
        ("/v2/template/{template_id}", "get"): "TemplateResponse",
        ("/v2/template/{template_id}/preview", "post"): "TemplatePreviewResponse",
    }
    for (path, method), definition in expected.items():
        schema = document["paths"][path][method]["responses"]["200"]["schema"]
        assert schema == {"$ref": f"#/definitions/{definition}"}, path


def test_no_operation_has_an_empty_success_schema(document):
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            for code, response in operation["responses"].items():
                if code.startswith("2"):
                    assert response.get("schema"), f"{method} {path} {code}"


# --- envelope variant --------------------------------------------------------


@pytest.fixture(scope="module")
def envelope_document():
    return build("notify-proxy.example.com", envelope=True)


def test_envelope_variant_is_valid(envelope_document):
    validate(envelope_document)


def test_envelope_operations_return_one_status_with_the_detail_inside(envelope_document):
    responses = envelope_document["paths"]["/v2/notifications/sms"]["post"]["responses"]
    # Notify's own codes move into the body, so the action never fails on them.
    assert set(responses) == {"200", "422"}
    schema = responses["200"]["schema"]
    assert set(schema["properties"]) == {"status_code", "success", "body", "errors"}
    assert schema["properties"]["status_code"]["type"] == "integer"
    assert schema["properties"]["errors"]["items"]["$ref"] == "#/definitions/NotifyErrorDetail"


def test_envelope_body_keeps_the_operations_own_schema(envelope_document):
    """Typed, so the designer still offers id, reference and the rest."""
    for path, method, definition in (
        ("/v2/notifications/sms", "post", "SmsResponse"),
        ("/v2/notifications/email", "post", "EmailResponse"),
        ("/v2/notifications/{notification_id}", "get", "NotificationResponse"),
        ("/v2/templates", "get", "TemplateListResponse"),
    ):
        schema = envelope_document["paths"][path][method]["responses"]["200"]["schema"]
        assert schema["properties"]["body"] == {"$ref": f"#/definitions/{definition}"}


def test_envelope_is_requested_on_every_proxy_operation(envelope_document):
    for path, operations in envelope_document["paths"].items():
        if not path.startswith("/v2/"):
            continue
        for method, operation in operations.items():
            headers = {p["name"]: p for p in operation["parameters"] if p["in"] == "header"}
            header = headers.get("X-Notify-Envelope")
            assert header is not None, f"missing on {method} {path}"
            assert header["default"] == "true"
            assert header["x-ms-visibility"] == "internal"


def test_the_letter_pdf_is_not_wrapped(envelope_document):
    """There is no sensible envelope for a PDF, so that operation is left alone."""
    operation = envelope_document["paths"]["/v2/notifications/{notification_id}/pdf"]["get"]
    assert operation["responses"]["200"]["schema"] == {"type": "string", "format": "binary"}


def test_service_routes_are_not_wrapped(envelope_document):
    """/health answers for itself; there is no Notify response to wrap."""
    operation = envelope_document["paths"]["/health"]["get"]
    assert operation["parameters"] == []
    assert operation["responses"]["200"]["schema"] == {"$ref": "#/definitions/HealthResponse"}


def test_envelope_combines_with_the_cloudflare_variant():
    document = build("notify.example.com", cf_access_client_id="abc.access", envelope=True)
    validate(document)
    headers = {
        p["name"]
        for p in document["paths"]["/v2/notifications/sms"]["post"]["parameters"]
        if p["in"] == "header"
    }
    assert headers == {"CF-Access-Client-Id", "X-Notify-Envelope"}


def test_no_envelope_header_without_the_flag(document):
    assert "X-Notify-Envelope" not in json.dumps(document)


def test_committed_file_is_the_passthrough_variant(document):
    """The default connector reports Notify's status codes as its own."""
    committed = json.loads(COMMITTED.read_text())
    assert "201" in committed["paths"]["/v2/notifications/sms"]["post"]["responses"]


def test_committed_file_matches_the_generator(document):
    """connector/swagger.json is generated — regenerate it after changing routes."""
    committed = json.loads(COMMITTED.read_text())
    assert committed == document, (
        "connector/swagger.json is out of date; "
        "run python scripts/build_connector_swagger.py"
    )
