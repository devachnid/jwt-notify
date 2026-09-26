"""The shared-secret check guarding the proxy routes and ``/token``."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Header, HTTPException, Request, status


async def require_proxy_key(
    request: Request,
    # Hidden from the schema: the connector supplies this from its configured
    # credential, so surfacing it per operation would only invite confusion.
    x_proxy_key: Annotated[str | None, Header(include_in_schema=False)] = None,
) -> None:
    """Check the shared secret, when one is configured.

    With no ``PROXY_KEY`` — only possible when ``PROXY_AUTH=none`` was set —
    the service trusts its front end (for example Cloudflare Access) to have
    authenticated the caller already.
    """
    expected: str | None = request.app.state.settings.proxy_key
    if expected is None:
        return

    # Compared as bytes: compare_digest raises TypeError on a str holding
    # anything outside ASCII, which a caller can send in a header. Starlette
    # decodes headers as latin-1, so encoding back that way recovers exactly
    # the bytes that were sent.
    if x_proxy_key is None or not hmac.compare_digest(
        x_proxy_key.encode("latin-1"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-Proxy-Key header",
        )
