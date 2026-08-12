#!/usr/bin/env python3
"""Generate the Swagger 2.0 definition for the Azure custom connector.

Azure Logic Apps and Power Platform custom connectors only import OpenAPI 2.0,
but FastAPI emits 3.1. This converts one to the other.

It is deliberately narrow: it understands the constructs this application's
schema actually contains and raises on anything else, so an unhandled construct
fails here rather than producing a connector that misbehaves in the designer.

    python scripts/build_connector_swagger.py --host notify-proxy.example.com

Writes to connector/swagger.json unless --output says otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Routes are registered regardless of configuration — the Notify API key is
# only read at startup — so the schema can be generated without any secrets.
from app.main import app  # noqa: E402


class UnsupportedConstruct(RuntimeError):
    """A schema construct this converter has not been taught to translate."""


def _strip_null(schema: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Collapse a 3.1 nullable union into a single schema plus a nullable flag.

    ``{"anyOf": [{"type": "string"}, {"type": "null"}]}`` becomes
    ``({"type": "string"}, True)``.
    """
    if "anyOf" not in schema:
        return schema, False

    branches = [b for b in schema["anyOf"] if b.get("type") != "null"]
    nullable = len(branches) != len(schema["anyOf"])
    merged = {k: v for k, v in schema.items() if k != "anyOf"}

    if len(branches) == 1:
        merged.update(branches[0])
        return merged, nullable

    # A union of primitives — "string or integer", say. Swagger 2.0 has no way
    # to say that, so the type is left off, which readers take as "any". Only
    # safe while the branches carry no structure of their own.
    if branches and all(set(b) <= {"type", "format"} for b in branches):
        return merged, nullable

    raise UnsupportedConstruct(
        f"anyOf with {len(branches)} structured branches cannot be expressed in "
        f"Swagger 2.0: {schema}"
    )


def convert_schema(schema: Any) -> Any:
    """Rewrite a 3.1 schema object as 2.0."""
    if isinstance(schema, list):
        return [convert_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    schema, nullable = _strip_null(schema)
    out: dict[str, Any] = {}

    for key, value in schema.items():
        if key == "$ref":
            out["$ref"] = value.replace("#/components/schemas/", "#/definitions/")
        elif key == "examples":
            # 3.1 takes a list here; 2.0 takes a single value.
            if isinstance(value, list) and value:
                out["example"] = value[0]
        elif key == "const":
            out["enum"] = [value]
        elif key in {"properties", "patternProperties"}:
            out[key] = {k: convert_schema(v) for k, v in value.items()}
        elif key in {"items", "additionalProperties"}:
            out[key] = convert_schema(value) if isinstance(value, dict) else value
        elif key in {"anyOf", "oneOf", "allOf"}:
            raise UnsupportedConstruct(f"{key} is not translated: {schema}")
        elif key in {"prefixItems", "contentMediaType", "if", "then", "else"}:
            raise UnsupportedConstruct(f"{key} is not translated: {schema}")
        else:
            out[key] = convert_schema(value) if isinstance(value, dict) else value

    if nullable:
        # Swagger 2.0 has no null type; x-nullable is the accepted extension.
        out["x-nullable"] = True

    return out


def convert_parameter(param: dict[str, Any]) -> dict[str, Any]:
    """Flatten a 3.x parameter (schema nested) into a 2.0 one (schema inline)."""
    schema = convert_schema(param.get("schema", {}))
    schema.pop("title", None)

    out: dict[str, Any] = {
        "name": param["name"],
        "in": param["in"],
        "required": param.get("required", param["in"] == "path"),
    }
    if param.get("description"):
        out["description"] = param["description"]

    # Give the Logic App designer a readable field label.
    out["x-ms-summary"] = param.get("description", param["name"]).split(".")[0][:80]

    for key in ("type", "format", "enum", "default", "minLength", "maxLength", "items"):
        if key in schema:
            out[key] = schema[key]

    if "$ref" in schema:
        raise UnsupportedConstruct(f"parameter {param['name']} references a definition")
    out.setdefault("type", "string")

    return out


def convert_operation(method: str, operation: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "operationId": operation.get("operationId", ""),
        "summary": operation.get("summary", ""),
        "description": operation.get("description", operation.get("summary", "")),
        "parameters": [convert_parameter(p) for p in operation.get("parameters", [])],
        "responses": {},
    }

    body = operation.get("requestBody")
    if body is not None:
        content = body.get("content", {})
        if "application/json" not in content:
            raise UnsupportedConstruct(f"request body is not JSON: {list(content)}")
        out["parameters"].append(
            {
                "name": "body",
                "in": "body",
                "required": body.get("required", True),
                "schema": convert_schema(content["application/json"]["schema"]),
            }
        )

    for code, response in operation.get("responses", {}).items():
        converted: dict[str, Any] = {"description": response.get("description", "")}
        content = response.get("content", {})
        json_content = content.get("application/json")
        if json_content and "schema" in json_content:
            converted["schema"] = convert_schema(json_content["schema"])
        elif "application/pdf" in content:
            # Swagger 2.0's own "type": "file" is rejected by strict schema
            # validators; the binary string form is understood everywhere.
            converted["schema"] = {"type": "string", "format": "binary"}
            out["produces"] = ["application/pdf"]
        out["responses"][str(code)] = converted

    if not out["responses"]:
        out["responses"]["default"] = {"description": "Response"}

    return out


#: Left out of the connector by default. Handing a connector the ability to
#: mint bare tokens invites the very pattern the proxy exists to avoid.
EXCLUDED_BY_DEFAULT = {"CreateToken"}


def build(host: str, scheme: str = "https", include_all: bool = False) -> dict[str, Any]:
    source = app.openapi()

    paths: dict[str, Any] = {}
    for path, operations in source["paths"].items():
        converted_ops = {}
        for method, operation in operations.items():
            if method not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            if not include_all and operation.get("operationId") in EXCLUDED_BY_DEFAULT:
                continue
            converted_ops[method] = convert_operation(method, operation)
        if converted_ops:
            paths[path] = converted_ops

    definitions = {
        name: convert_schema(schema)
        for name, schema in source.get("components", {}).get("schemas", {}).items()
    }

    return {
        "swagger": "2.0",
        "info": {
            "title": source["info"]["title"],
            "description": source["info"].get("description", ""),
            "version": source["info"]["version"],
        },
        "host": host,
        "basePath": "/",
        "schemes": [scheme],
        "consumes": ["application/json"],
        "produces": ["application/json"],
        "securityDefinitions": {
            "proxyKey": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Proxy-Key",
                "description": (
                    "Shared secret for this proxy. Leave the connector's API key "
                    "blank if the service is protected by Cloudflare Access instead."
                ),
            }
        },
        "security": [{"proxyKey": []}],
        "paths": paths,
        "definitions": definitions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        default="notify-proxy.example.com",
        help="Host the connector will call. Change it to your own before importing.",
    )
    parser.add_argument("--scheme", default="https", choices=["https", "http"])
    parser.add_argument("--output", type=Path, default=Path("connector/swagger.json"))
    parser.add_argument(
        "--all",
        action="store_true",
        dest="include_all",
        help=f"Also include operations left out by default ({', '.join(sorted(EXCLUDED_BY_DEFAULT))}).",
    )
    args = parser.parse_args()

    document = build(args.host, args.scheme, args.include_all)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2) + "\n")
    print(f"wrote {args.output} ({len(document['paths'])} paths, host {args.host})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
