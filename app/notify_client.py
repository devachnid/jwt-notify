"""Thin async client that forwards requests to GOV.UK Notify.

A fresh JWT is minted for every request, so the 30 second expiry that makes
Notify awkward to call from a static-credential client is never a concern.
"""

from __future__ import annotations

from typing import Any

import httpx

from .api_key import ApiKey
from .tokens import create_token


class NotifyUnavailableError(RuntimeError):
    """Notify could not be reached, or did not answer in time."""

    def __init__(self, message: str, *, timeout: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout


class NotifyClient:
    def __init__(
        self,
        api_key: ApiKey,
        base_url: str,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={"User-Agent": "jwt-notify-proxy"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> httpx.Response:
        """Forward a request to Notify, signing it with a freshly minted JWT.

        Returns the upstream response untouched — including error responses,
        which callers are expected to pass through so that Notify's own
        validation messages reach the client.
        """
        token, _ = create_token(self._api_key)
        headers = {"Authorization": f"Bearer {token}"}

        # Drop unset query parameters rather than sending them empty.
        query = None if params is None else {k: v for k, v in params.items() if v is not None}

        try:
            return await self._client.request(
                method, path, params=query, json=json, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise NotifyUnavailableError(
                "Timed out waiting for GOV.UK Notify", timeout=True
            ) from exc
        except httpx.HTTPError as exc:
            # The message from httpx names the host but never the credentials.
            raise NotifyUnavailableError(f"Could not reach GOV.UK Notify: {exc}") from exc
