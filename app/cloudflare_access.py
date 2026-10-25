"""Verification of the token Cloudflare Access attaches to each request.

Access signs a JWT (RS256) for every request it lets through and sends it to
the origin as ``Cf-Access-Jwt-Assertion``. Checking it here means a request
that reaches the service some other way — straight to the port, around the
tunnel — is refused rather than trusted.

See https://developers.cloudflare.com/cloudflare-one/identity/authorization-cookie/validating-json/
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

import httpx
import jwt

from .config import CloudflareAccess

logger = logging.getLogger("jwt-notify")

#: Access rotates its signing keys every six weeks, publishing the new one
#: ahead of use, so an hourly refresh is plenty.
KEYS_MAX_AGE_SECONDS = 3600
#: A token naming a key we have not seen triggers a refresh, but no more often
#: than this — otherwise made-up key ids would have us hammer the certs URL.
MIN_REFRESH_INTERVAL_SECONDS = 60
#: Allowance for clock skew between Cloudflare and this host.
LEEWAY_SECONDS = 30


class AccessDenied(Exception):
    """The token is missing, malformed, or not one Access issued for this app."""


class AccessUnavailable(Exception):
    """Access's public keys could not be fetched, so nothing can be verified."""


class AccessVerifier:
    def __init__(
        self,
        access: CloudflareAccess,
        timeout: float,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._access = access
        self._http = httpx.AsyncClient(
            timeout=timeout, transport=transport, follow_redirects=False
        )
        self._clock = clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._attempted_at: float | None = None
        self._lock = asyncio.Lock()

    async def verify(self, token: str) -> None:
        """Return if ``token`` is valid for this Access application, else raise."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AccessDenied from exc
        kid = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(kid, str):
            raise AccessDenied

        key = await self._key(kid)
        if key is None:
            raise AccessDenied
        try:
            jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self._access.aud,
                issuer=self._access.issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise AccessDenied from exc

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _key(self, kid: str) -> jwt.PyJWK | None:
        if kid in self._keys and not self._stale():
            return self._keys[kid]
        async with self._lock:
            # Another request may have refreshed while this one waited.
            if kid in self._keys and not self._stale():
                return self._keys[kid]
            if (
                self._attempted_at is None
                or self._clock() - self._attempted_at >= MIN_REFRESH_INTERVAL_SECONDS
            ):
                await self._refresh()
            elif not self._keys:
                raise AccessUnavailable
        return self._keys.get(kid)

    def _stale(self) -> bool:
        return (
            self._fetched_at is None
            or self._clock() - self._fetched_at >= KEYS_MAX_AGE_SECONDS
        )

    async def _refresh(self) -> None:
        self._attempted_at = self._clock()
        try:
            response = await self._http.get(self._access.certs_url)
            response.raise_for_status()
            keys = {}
            for data in response.json().get("keys", []):
                try:
                    key = jwt.PyJWK(data, algorithm="RS256")
                except jwt.PyJWTError:
                    continue
                if isinstance(data.get("kid"), str):
                    keys[data["kid"]] = key
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            if not self._keys:
                raise AccessUnavailable from exc
            # Keep verifying with the keys we have; try again shortly.
            logger.warning("could not refresh Cloudflare Access keys: %s", exc)
            return
        self._keys = keys
        self._fetched_at = self._clock()
