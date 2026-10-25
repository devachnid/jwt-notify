"""Runtime configuration, read from the environment."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .api_key import ApiKey, InvalidApiKeyError, parse_api_key

DEFAULT_NOTIFY_BASE_URL = "https://api.notifications.service.gov.uk"

#: Large enough for a precompiled letter — Notify caps the PDF at 2MB, which
#: base64 grows to about 2.7MB — with room to spare.
DEFAULT_MAX_REQUEST_BYTES = 5 * 1024 * 1024

#: How long a caller has to send its whole request body.
DEFAULT_BODY_READ_TIMEOUT_SECONDS = 30.0

#: ``PROXY_AUTH`` value that runs without ``PROXY_KEY`` and instead checks the
#: token Cloudflare Access attaches to every request it lets through.
PROXY_AUTH_CLOUDFLARE_ACCESS = "cloudflare-access"

_TRUTHY = {"1", "true", "yes", "on"}

_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")


@dataclass(frozen=True)
class CloudflareAccess:
    """The Access application whose tokens the service accepts."""

    #: The team domain, e.g. ``myteam.cloudflareaccess.com``.
    team_domain: str
    #: The application's Audience (AUD) tag.
    aud: str

    @property
    def issuer(self) -> str:
        return f"https://{self.team_domain}"

    @property
    def certs_url(self) -> str:
        return f"{self.issuer}/cdn-cgi/access/certs"


@dataclass(frozen=True)
class Settings:
    #: The Notify API key the proxy signs with. ``None`` disables the proxy
    #: routes; the ``/token`` endpoint still works, since it is given a key.
    notify_api_key: ApiKey | None
    notify_base_url: str
    request_timeout: float
    #: Shared secret required in ``X-Proxy-Key`` on the proxy routes and
    #: ``/token``. ``None`` only in Cloudflare Access mode.
    proxy_key: str | None
    #: Request bodies larger than this are refused with 413.
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES
    #: Set in ``PROXY_AUTH=cloudflare-access`` mode instead of ``proxy_key``.
    #: With neither set, every protected request is refused.
    cloudflare_access: CloudflareAccess | None = None
    #: A request body not fully received within this many seconds gets 408.
    body_read_timeout: float = DEFAULT_BODY_READ_TIMEOUT_SECONDS

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
    cloudflare_access: CloudflareAccess | None = None
    if proxy_auth == "none":
        # The old open mode trusted whoever reached the port to have passed
        # Access first, which nothing checked.
        raise ConfigurationError(
            "PROXY_AUTH=none is no longer supported. Behind Cloudflare Access, "
            "set PROXY_AUTH=cloudflare-access with CF_ACCESS_TEAM_DOMAIN and "
            "CF_ACCESS_AUD so the service checks Access's token itself."
        )
    if proxy_auth not in ("", "key", PROXY_AUTH_CLOUDFLARE_ACCESS):
        raise ConfigurationError(
            f"PROXY_AUTH must be 'key' or 'cloudflare-access', got {proxy_auth!r}"
        )
    if proxy_auth == PROXY_AUTH_CLOUDFLARE_ACCESS:
        if proxy_key is not None:
            raise ConfigurationError(
                "PROXY_AUTH=cloudflare-access and PROXY_KEY are both set: unset one of them"
            )
        cloudflare_access = _load_cloudflare_access(environ)
    elif proxy_key is None:
        # Fail closed: an open proxy sends messages as your Notify service for
        # anyone who can reach it.
        raise ConfigurationError(
            "PROXY_KEY is not set. Set it to a long random secret, or set "
            "PROXY_AUTH=cloudflare-access if Cloudflare Access guards this service."
        )

    raw_read_timeout = environ.get(
        "BODY_READ_TIMEOUT_SECONDS", str(DEFAULT_BODY_READ_TIMEOUT_SECONDS)
    ).strip()
    try:
        body_read_timeout = float(raw_read_timeout)
    except ValueError as exc:
        raise ConfigurationError(
            f"BODY_READ_TIMEOUT_SECONDS must be a number, got {raw_read_timeout!r}"
        ) from exc
    if body_read_timeout <= 0:
        raise ConfigurationError("BODY_READ_TIMEOUT_SECONDS must be greater than zero")

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
        cloudflare_access=cloudflare_access,
        body_read_timeout=body_read_timeout,
    )


def _load_cloudflare_access(environ) -> CloudflareAccess:
    domain = environ.get("CF_ACCESS_TEAM_DOMAIN", "").strip().lower()
    domain = domain.removeprefix("https://").rstrip("/")
    if "." not in domain:
        # Just the team name, as shown in Zero Trust settings.
        domain = f"{domain}.cloudflareaccess.com" if domain else ""
    if not _HOSTNAME.match(domain):
        raise ConfigurationError(
            "CF_ACCESS_TEAM_DOMAIN must be your Access team domain, e.g. "
            "myteam.cloudflareaccess.com"
        )
    aud = environ.get("CF_ACCESS_AUD", "").strip()
    if not aud:
        raise ConfigurationError(
            "CF_ACCESS_AUD must be set to the Access application's Audience (AUD) tag"
        )
    return CloudflareAccess(team_domain=domain, aud=aud)


def docs_enabled(env: dict[str, str] | None = None) -> bool:
    """Whether to serve ``/docs`` and ``/openapi.json`` (off unless ``ENABLE_DOCS``)."""
    environ = os.environ if env is None else env
    return environ.get("ENABLE_DOCS", "").strip().lower() in _TRUTHY
