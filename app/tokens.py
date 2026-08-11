"""Minimal HS256 JWT creation for GOV.UK Notify.

Notify expects a token with the header::

    {"typ": "JWT", "alg": "HS256"}

and the payload::

    {"iss": "<service id>", "iat": <epoch seconds, UTC>}

Signing is HMAC-SHA256 over ``base64url(header).base64url(payload)`` using the
secret key from the API key, so only the standard library is needed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from .api_key import ApiKey

#: Notify rejects tokens whose ``iat`` is more than this many seconds old.
TOKEN_LIFETIME_SECONDS = 30

_HEADER = {"typ": "JWT", "alg": "HS256"}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

def _encode_segment(payload: dict) -> str:
    # Compact, deterministic JSON; key order is preserved as written.
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _b64url(raw)


def create_token(api_key: ApiKey, issued_at: int | None = None) -> tuple[str, int]:
    """Create a Notify JWT.

    Args:
        api_key: The parsed API key providing the issuer and signing secret.
        issued_at: ``iat`` in epoch seconds; defaults to the current UTC time.

    Returns:
        A ``(token, issued_at)`` tuple.
    """
    iat = int(time.time()) if issued_at is None else int(issued_at)

    header_segment = _encode_segment(_HEADER)
    payload_segment = _encode_segment({"iss": api_key.iss, "iat": iat})
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")

    signature = hmac.new(
        api_key.secret_key.encode("utf-8"), signing_input, hashlib.sha256
    ).digest()

    return f"{header_segment}.{payload_segment}.{_b64url(signature)}", iat
