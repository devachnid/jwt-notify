import pytest

from app.api_key import InvalidApiKeyError, parse_api_key

ISS = "26785a09-ab16-4eb0-8407-a37497a57506"
SECRET = "3d844edf-8d35-48ac-975b-e847b4f122b0"
EXAMPLE_KEY = f"my_test_key-{ISS}-{SECRET}"


def test_parses_the_documented_example():
    key = parse_api_key(EXAMPLE_KEY)
    assert key.key_name == "my_test_key"
    assert key.iss == ISS
    assert key.secret_key == SECRET


def test_key_name_may_contain_hyphens():
    key = parse_api_key(f"my-test-key-{ISS}-{SECRET}")
    assert key.key_name == "my-test-key"
    assert key.iss == ISS
    assert key.secret_key == SECRET


def test_key_name_may_look_like_a_uuid():
    name = "11111111-2222-3333-4444-555555555555"
    key = parse_api_key(f"{name}-{ISS}-{SECRET}")
    assert key.key_name == name
    assert key.iss == ISS


def test_surrounding_whitespace_is_ignored():
    assert parse_api_key(f"  {EXAMPLE_KEY}\n").key_name == "my_test_key"


@pytest.mark.parametrize(
    "bad_key",
    [
        "",
        "   ",
        "my_test_key",
        f"my_test_key-{ISS}",  # missing secret key
        f"{ISS}-{SECRET}",  # missing key name
        f"my_test_key-not-a-uuid-{SECRET}",
        f"my_test_key-{ISS}-3d844edf8d3548ac975be847b4f122b0",  # unhyphenated secret
        f"my_test_key-{ISS}-{SECRET}extra",
        f"my_test_key-{ISS}-{SECRET[:-1]}g",  # non-hex character
    ],
)
def test_rejects_malformed_keys(bad_key):
    with pytest.raises(InvalidApiKeyError):
        parse_api_key(bad_key)


def test_error_message_does_not_leak_the_key():
    with pytest.raises(InvalidApiKeyError) as excinfo:
        parse_api_key(f"my_test_key-{ISS}-nonsense")
    assert SECRET not in str(excinfo.value)
    assert "nonsense" not in str(excinfo.value)
