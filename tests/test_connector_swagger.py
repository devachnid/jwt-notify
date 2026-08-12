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


def test_committed_file_matches_the_generator(document):
    """connector/swagger.json is generated — regenerate it after changing routes."""
    committed = json.loads(COMMITTED.read_text())
    assert committed == document, (
        "connector/swagger.json is out of date; "
        "run python scripts/build_connector_swagger.py"
    )
