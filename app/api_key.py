"""Parsing of GOV.UK Notify API keys.

A Notify API key has the form ``{key_name}-{iss-uuid}-{secret-key-uuid}``, e.g.::

    my_test_key-26785a09-ab16-4eb0-8407-a37497a57506-3d844edf-8d35-48ac-975b-e847b4f122b0

The key name itself may contain hyphens, so the two UUIDs are taken from the end
of the string rather than by splitting on ``-`` from the left.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

UUID_LENGTH = 36
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class InvalidApiKeyError(ValueError):
    """Raised when an API key does not match the Notify key format."""


@dataclass(frozen=True)
class ApiKey:
    """The three components of a Notify API key."""

    key_name: str
    iss: str
    secret_key: str


def _is_uuid(value: str) -> bool:
    if not _UUID_RE.match(value):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def parse_api_key(api_key: str) -> ApiKey:
    """Split a Notify API key into its key name, issuer and secret key.

    Raises:
        InvalidApiKeyError: if the key is not of the form
            ``{key_name}-{iss-uuid}-{secret-key-uuid}``.
    """
    if not isinstance(api_key, str):
        raise InvalidApiKeyError("api_key must be a string")

    key = api_key.strip()
    if not key:
        raise InvalidApiKeyError("api_key must not be empty")

    # Trailing UUID: the secret key.
    secret_key = key[-UUID_LENGTH:]
    remainder = key[: -UUID_LENGTH - 1]
    if len(key) <= UUID_LENGTH or key[-UUID_LENGTH - 1] != "-" or not _is_uuid(secret_key):
        raise InvalidApiKeyError(
            "api_key must end with a secret key UUID "
            "(expected format: {key_name}-{iss-uuid}-{secret-key-uuid})"
        )

    # Next UUID back: the issuer (service id).
    iss = remainder[-UUID_LENGTH:]
    key_name = remainder[: -UUID_LENGTH - 1]
    if (
        len(remainder) <= UUID_LENGTH
        or remainder[-UUID_LENGTH - 1] != "-"
        or not _is_uuid(iss)
    ):
        raise InvalidApiKeyError(
            "api_key must contain a service id UUID before the secret key "
            "(expected format: {key_name}-{iss-uuid}-{secret-key-uuid})"
        )

    if not key_name:
        raise InvalidApiKeyError("api_key must start with a key name")

    return ApiKey(key_name=key_name, iss=iss, secret_key=secret_key)
