"""Inbound authentication for the proxy routes and ``/token``.

The check runs in ASGI middleware, ahead of everything else, so a caller that
is not authenticated never gets the service to read, buffer or parse its
request body. It accepts one of:

* ``X-Proxy-Key`` matching ``PROXY_KEY`` (the default), or
* in ``PROXY_AUTH=cloudflare-access`` mode, a ``Cf-Access-Jwt-Assertion`` that
  Cloudflare Access signed for this application.

Routes still declare :func:`require_authenticated`, which only confirms the
middleware passed the request — so a protected route mounted somewhere the
middleware does not look fails closed instead of open.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import HTTPException, Request, status
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .cloudflare_access import AccessDenied, AccessUnavailable, AccessVerifier

logger = logging.getLogger("jwt-notify")

PROXY_KEY_HEADER = "x-proxy-key"
ACCESS_TOKEN_HEADER = "cf-access-jwt-assertion"

#: Key in the ASGI scope's ``state`` marking a request as authenticated.
AUTHENTICATED = "authenticated"


def is_protected(path: str) -> bool:
    return path == "/token" or path == "/v2" or path.startswith("/v2/")


def is_authenticated(scope: Scope) -> bool:
    return bool(scope.get("state", {}).get(AUTHENTICATED))


class AuthenticationMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_protected(scope["path"]):
            await self.app(scope, receive, send)
            return

        state = scope["app"].state
        refusal = await _authenticate(
            Headers(scope=scope),
            state.settings,
            getattr(state, "access_verifier", None),
        )
        if refusal is not None:
            await refusal(scope, receive, send)
            return

        scope.setdefault("state", {})[AUTHENTICATED] = True
        await self.app(scope, receive, send)


async def _authenticate(headers: Headers, settings, verifier: AccessVerifier | None):
    """Return ``None`` if the caller is authenticated, else the response to send."""
    if settings.proxy_key is not None:
        sent = headers.get(PROXY_KEY_HEADER)
        # Compared as bytes: compare_digest raises TypeError on a str holding
        # anything outside ASCII, which a caller can send in a header. Starlette
        # decodes headers as latin-1, so encoding back that way recovers exactly
        # the bytes that were sent.
        if sent is not None and hmac.compare_digest(
            sent.encode("latin-1"), settings.proxy_key.encode("utf-8")
        ):
            return None
        return _refuse("Missing or invalid X-Proxy-Key header")

    if settings.cloudflare_access is not None and verifier is not None:
        try:
            await verifier.verify(headers.get(ACCESS_TOKEN_HEADER, ""))
        except AccessDenied:
            return _refuse("Missing or invalid Cloudflare Access token")
        except AccessUnavailable:
            logger.error("cannot fetch Cloudflare Access keys; refusing requests")
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": "Cannot verify the Cloudflare Access token right now"},
            )
        return None

    # No way to authenticate anyone: refuse rather than fall open.
    return _refuse("Authentication is not configured")


def _refuse(detail: str) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": detail})


async def require_authenticated(request: Request) -> None:
    """Route-level backstop: the request must have passed the middleware."""
    if not is_authenticated(request.scope):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )
