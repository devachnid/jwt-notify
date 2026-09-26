"""Request body size limit.

Plain ASGI middleware, so an oversized body is refused before the application
sees it — including a chunked body that declares no Content-Length. The body is
read here, up to the limit, and replayed to the application; every route
buffers its body in full anyway, so this costs no extra memory in practice.
"""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import DEFAULT_MAX_REQUEST_BYTES


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = getattr(scope["app"].state, "settings", None)
        limit = getattr(settings, "max_request_bytes", DEFAULT_MAX_REQUEST_BYTES)

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0
                if declared > limit:
                    await self._reject(scope, receive, send, limit)
                    return

        chunks: list[bytes] = []
        received = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            received += len(body)
            if received > limit:
                await self._reject(scope, receive, send, limit)
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": f"Request body exceeds {limit} bytes"},
        )
        await response(scope, receive, send)
