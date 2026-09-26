"""Runtime configuration, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .api_key import ApiKey, InvalidApiKeyError, parse_api_key

DEFAULT_NOTIFY_BASE_URL = "https://api.notifications.service.gov.uk"

#: Large enough for a precompiled letter — Notify caps the PDF at 2MB, which
#: base64 grows to about 2.7MB — with room to spare.
DEFAULT_MAX_REQUEST_BYTES = 5 * 1024 * 1024

#: ``PROXY_AUTH`` value that runs without ``PROXY_KEY``, for use behind an
#: authenticating front end such as Cloudflare Access.
PROXY_AUTH_NONE = "none"

_TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    #: The Notify API key the proxy signs with. ``None`` disables the proxy
    #: routes; the ``/token`` endpoint still works, since it is given a key.
    notify_api_key: ApiKey | None
    notify_base_url: str
    request_timeout: float
    #: Shared secret required in ``X-Proxy-Key`` on the proxy routes and
    #: ``/token``. ``None`` leaves them open; :func:`load_settings` only allows
    #: that when ``PROXY_AUTH=none`` says so explicitly.
    proxy_key: str | None
    #: Request bodies larger than this are refused with 413.
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES

    @property
    def proxy_enabled(self) -> bool:
        return self.notify_api_key is not None


class ConfigurationError(RuntimeError):
    """Raised when the environment holds a value the service cannot use."""


def load_settings(env: dict[str, str] | None = None) -> Settings:
    environ = os.environ if env is None else env

    raw_key = environ.get("NOTIFY_API_KEY", "").strip()
    api_key: ApiKey | None = None
    if raw_key:
        try:
            api_key = parse_api_key(raw_key)
        except InvalidApiKeyError as exc:
            # str(exc) never contains the key itself.
            raise ConfigurationError(f"NOTIFY_API_KEY is not a valid Notify API key: {exc}") from exc

    raw_timeout = environ.get("NOTIFY_TIMEOUT_SECONDS", "30").strip()
    try:
        timeout = float(raw_timeout)
    except ValueError as exc:
        raise ConfigurationError(
            f"NOTIFY_TIMEOUT_SECONDS must be a number, got {raw_timeout!r}"
        ) from exc
    if timeout <= 0:
        raise ConfigurationError("NOTIFY_TIMEOUT_SECONDS must be greater than zero")

    proxy_key = environ.get("PROXY_KEY", "").strip() or None
    proxy_auth = environ.get("PROXY_AUTH", "").strip().lower()
    if proxy_auth not in ("", "key", PROXY_AUTH_NONE):
        raise ConfigurationError(
            f"PROXY_AUTH must be 'key' or 'none', got {proxy_auth!r}"
        )
    if proxy_auth == PROXY_AUTH_NONE:
        if proxy_key is not None:
            raise ConfigurationError(
                "PROXY_AUTH=none and PROXY_KEY are both set: unset one of them"
            )
    elif proxy_key is None:
        # Fail closed: an open proxy sends messages as your Notify service for
        # anyone who can reach it, so it has to be asked for by name.
        raise ConfigurationError(
            "PROXY_KEY is not set. Set it to a long random secret, or set "
            "PROXY_AUTH=none if an authenticating front end (such as "
            "Cloudflare Access) already guards this service."
        )

    raw_max = environ.get("MAX_REQUEST_BYTES", str(DEFAULT_MAX_REQUEST_BYTES)).strip()
    try:
        max_request_bytes = int(raw_max)
    except ValueError as exc:
        raise ConfigurationError(
            f"MAX_REQUEST_BYTES must be a whole number, got {raw_max!r}"
        ) from exc
    if max_request_bytes <= 0:
        raise ConfigurationError("MAX_REQUEST_BYTES must be greater than zero")

    return Settings(
        notify_api_key=api_key,
        notify_base_url=environ.get("NOTIFY_BASE_URL", DEFAULT_NOTIFY_BASE_URL).rstrip("/"),
        request_timeout=timeout,
        proxy_key=proxy_key,
        max_request_bytes=max_request_bytes,
    )


def docs_enabled(env: dict[str, str] | None = None) -> bool:
    """Whether to serve ``/docs`` and ``/openapi.json`` (off unless ``ENABLE_DOCS``)."""
    environ = os.environ if env is None else env
    return environ.get("ENABLE_DOCS", "").strip().lower() in _TRUTHY
