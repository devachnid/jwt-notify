"""HTTP service that turns a GOV.UK Notify API key into a signed JWT."""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .api_key import InvalidApiKeyError, parse_api_key
from .tokens import TOKEN_LIFETIME_SECONDS, create_token

app = FastAPI(
    title="Notify JWT service",
    description=(
        "Generates the short-lived JWTs required to call the GOV.UK Notify API. "
        "POST a Notify API key and receive a bearer token valid for "
        f"{TOKEN_LIFETIME_SECONDS} seconds."
    ),
    version="1.0.0",
)


class TokenRequest(BaseModel):
    api_key: str = Field(
        ...,
        min_length=1,
        description="A Notify API key: {key_name}-{iss-uuid}-{secret-key-uuid}",
    )


class TokenResponse(BaseModel):
    token: str
    key_name: str
    iss: str
    iat: int
    expires_in: int


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return validation errors without echoing the submitted body.

    The request body contains a secret, so the default handler's ``input`` and
    ``ctx`` fields are dropped.
    """
    errors = [
        {"loc": error.get("loc", []), "msg": error.get("msg", ""), "type": error.get("type", "")}
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.exception_handler(InvalidApiKeyError)
async def invalid_api_key_handler(
    request: Request, exc: InvalidApiKeyError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)}
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/token", response_model=TokenResponse)
async def token(request: TokenRequest) -> TokenResponse:
    """Generate a Notify JWT from an API key."""
    api_key = parse_api_key(request.api_key)
    jwt, iat = create_token(api_key)
    return TokenResponse(
        token=jwt,
        key_name=api_key.key_name,
        iss=api_key.iss,
        iat=iat,
        expires_in=TOKEN_LIFETIME_SECONDS,
    )
