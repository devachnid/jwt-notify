"""Runtime configuration, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .api_key import ApiKey, InvalidApiKeyError, parse_api_key

DEFAULT_NOTIFY_BASE_URL = "https://api.notifications.service.gov.uk"


@dataclass(frozen=True)
class Settings:
    #: The Notify API key the proxy signs with. ``None`` disables the proxy
    #: routes; the ``/token`` endpoint still works, since it is given a key.
    notify_api_key: ApiKey | None
    notify_base_url: str
    request_timeout: float
    #: Shared secret required in ``X-Proxy-Key`` on proxy routes. ``None``
    #: leaves the proxy open, which is only safe behind an authenticating
    #: front end such as Cloudflare Access.
    proxy_key: str | None

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

    return Settings(
        notify_api_key=api_key,
        notify_base_url=environ.get("NOTIFY_BASE_URL", DEFAULT_NOTIFY_BASE_URL).rstrip("/"),
        request_timeout=timeout,
        proxy_key=proxy_key,
    )
