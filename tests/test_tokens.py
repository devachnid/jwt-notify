import base64
import json
import time

import jwt as pyjwt  # dev-only cross-check against a reference implementation

from app.api_key import parse_api_key
from app.tokens import TOKEN_LIFETIME_SECONDS, create_token

ISS = "26785a09-ab16-4eb0-8407-a37497a57506"
SECRET = "3d844edf-8d35-48ac-975b-e847b4f122b0"
API_KEY = parse_api_key(f"my_test_key-{ISS}-{SECRET}")


def _decode_segment(segment: str) -> dict:
    padding = "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment + padding))


def test_header_matches_the_notify_specification():
    token, _ = create_token(API_KEY)
    header = _decode_segment(token.split(".")[0])
    assert header == {"typ": "JWT", "alg": "HS256"}


def test_payload_matches_the_notify_specification():
    token, iat = create_token(API_KEY, issued_at=1568818578)
    payload = _decode_segment(token.split(".")[1])
    assert payload == {"iss": ISS, "iat": 1568818578}
    assert iat == 1568818578


def test_iat_defaults_to_now_in_epoch_seconds():
    before = int(time.time())
    _, iat = create_token(API_KEY)
    after = int(time.time())
    assert before <= iat <= after


def test_token_has_no_padding_and_three_segments():
    token, _ = create_token(API_KEY)
    assert token.count(".") == 2
    assert "=" not in token


def test_signature_verifies_with_the_secret_key():
    token, iat = create_token(API_KEY, issued_at=1568818578)
    decoded = pyjwt.decode(
        token, SECRET, algorithms=["HS256"], options={"verify_exp": False}
    )
    assert decoded == {"iss": ISS, "iat": 1568818578}


def test_signature_fails_with_the_wrong_secret_key():
    token, _ = create_token(API_KEY)
    try:
        pyjwt.decode(token, "0" * 36, algorithms=["HS256"])
    except pyjwt.InvalidSignatureError:
        return
    raise AssertionError("token verified with the wrong secret key")


def test_token_expires_within_thirty_seconds():
    assert TOKEN_LIFETIME_SECONDS == 30
