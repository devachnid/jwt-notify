"""HTTP service that signs GOV.UK Notify requests.

Two things are on offer:

* ``POST /token`` turns a Notify API key into a signed JWT.
* the ``/v2/...`` routes proxy the Notify API itself, minting a fresh JWT for
  every call. That is the useful half for a client that can only hold a static
  credential — an Azure Logic App custom connector, say — because Notify's
  tokens expire 30 seconds after they are issued.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .api_key import InvalidApiKeyError, parse_api_key
from .auth import require_proxy_key
from .config import docs_enabled, load_settings
from .limits import BodySizeLimitMiddleware
from .models import HealthResponse
from .notify_client import NotifyClient
from .routes_notify import router as notify_router
from .tokens import TOKEN_LIFETIME_SECONDS, create_token

logger = logging.getLogger("jwt-notify")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app.state.settings = settings
    app.state.notify_client = None

    if settings.proxy_enabled:
        assert settings.notify_api_key is not None
        app.state.notify_client = NotifyClient(
            settings.notify_api_key,
            settings.notify_base_url,
            settings.request_timeout,
        )
        logger.info(
            "proxying to %s as service %s",
            settings.notify_base_url,
            settings.notify_api_key.iss,
        )
    else:
        logger.warning("NOTIFY_API_KEY is not set: the /v2 proxy routes will return 503")

    if settings.proxy_key is None:
        logger.warning(
            "PROXY_AUTH=none: the proxy routes and /token are open to anyone who "
            "can reach this service. Only do this behind an authenticating "
            "front end."
        )

    try:
        yield
    finally:
        if app.state.notify_client is not None:
            await app.state.notify_client.aclose()


app = FastAPI(
    title="Notify JWT service",
    description=(
        "Signs requests to the GOV.UK Notify API. The /v2 routes mirror Notify's "
        "own and add the JWT, so a caller that can only hold a static credential "
        f"never has to deal with a token that expires after {TOKEN_LIFETIME_SECONDS} "
        "seconds. POST /token returns a bare token instead, for callers that want "
        "to make the onward call themselves."
    ),
    version="2.0.0",
    lifespan=lifespan,
    # The schema is still generated for the connector build; it is only not
    # served, unless ENABLE_DOCS asks for it.
    **(
        {}
        if docs_enabled()
        else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    ),
)
app.add_middleware(BodySizeLimitMiddleware)


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


@app.get(
    "/health", response_model=HealthResponse, operation_id="GetHealth", tags=["Service"]
)
async def health(request: Request) -> HealthResponse:
    return HealthResponse(
        status="ok", proxy_enabled=request.app.state.settings.proxy_enabled
    )


@app.post(
    "/token",
    response_model=TokenResponse,
    operation_id="CreateToken",
    tags=["Service"],
    dependencies=[Depends(require_proxy_key)],
)
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


app.include_router(notify_router)
