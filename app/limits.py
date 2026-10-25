"""Request body size and read-time limits.

Plain ASGI middleware, so an oversized body is refused before the application
sees it — including a chunked body that declares no Content-Length. The body is
read here, up to the limit, and replayed to the application.

It runs after :class:`app.auth.AuthenticationMiddleware`, so the full
``MAX_REQUEST_BYTES`` allowance only applies to authenticated requests; anyone
else (``/health``, unknown paths) gets a few kilobytes. Reading has a deadline
too, so a caller cannot hold memory by sending part of a body and stalling.
"""

from __future__ import annotations

import asyncio

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import is_authenticated
from .config import DEFAULT_BODY_READ_TIMEOUT_SECONDS, DEFAULT_MAX_REQUEST_BYTES

#: Body allowance for requests that did not authenticate. Nothing they can
#: reach takes a body.
UNAUTHENTICATED_MAX_REQUEST_BYTES = 4096


class _Disconnected(Exception):
    pass


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = getattr(scope["app"].state, "settings", None)
        limit = getattr(settings, "max_request_bytes", DEFAULT_MAX_REQUEST_BYTES)
        if not is_authenticated(scope):
            limit = min(limit, UNAUTHENTICATED_MAX_REQUEST_BYTES)
        deadline = getattr(settings, "body_read_timeout", DEFAULT_BODY_READ_TIMEOUT_SECONDS)

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0
                if declared > limit:
                    await self._reject(scope, receive, send, limit)
                    return

        try:
            async with asyncio.timeout(deadline):
                chunks = await self._read(receive, limit)
        except TimeoutError:
            await JSONResponse(
                status_code=408,
                content={"detail": f"Request body not received within {deadline:g} seconds"},
                headers={"Connection": "close"},
            )(scope, receive, send)
            return
        except _Disconnected:
            return
        if chunks is None:
            await self._reject(scope, receive, send, limit)
            return

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    async def _read(receive: Receive, limit: int) -> list[bytes] | None:
        """The body's chunks, or ``None`` once it passes ``limit``."""
        chunks: list[bytes] = []
        received = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                raise _Disconnected
            body = message.get("body", b"")
            received += len(body)
            if received > limit:
                return None
            chunks.append(body)
            if not message.get("more_body", False):
                return chunks

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": f"Request body exceeds {limit} bytes"},
        )
        await response(scope, receive, send)
