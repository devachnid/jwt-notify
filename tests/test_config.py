import pytest

from app.config import DEFAULT_MAX_REQUEST_BYTES, ConfigurationError, docs_enabled, load_settings

API_KEY = (
    "my_test_key-26785a09-ab16-4eb0-8407-a37497a57506-3d844edf-8d35-48ac-975b-e847b4f122b0"
)


def test_refuses_to_start_without_a_proxy_key():
    with pytest.raises(ConfigurationError, match="PROXY_AUTH=none"):
        load_settings({"NOTIFY_API_KEY": API_KEY})


def test_refuses_to_start_without_a_proxy_key_in_token_only_mode():
    with pytest.raises(ConfigurationError):
        load_settings({})


def test_proxy_key_is_used():
    settings = load_settings({"NOTIFY_API_KEY": API_KEY, "PROXY_KEY": "s3cret"})
    assert settings.proxy_key == "s3cret"


@pytest.mark.parametrize("value", ["none", "NONE", " none "])
def test_proxy_auth_none_opens_the_proxy(value):
    settings = load_settings({"NOTIFY_API_KEY": API_KEY, "PROXY_AUTH": value})
    assert settings.proxy_key is None


def test_proxy_auth_none_conflicts_with_a_proxy_key():
    with pytest.raises(ConfigurationError, match="both set"):
        load_settings({"PROXY_AUTH": "none", "PROXY_KEY": "s3cret"})


def test_proxy_auth_key_still_needs_the_key():
    with pytest.raises(ConfigurationError):
        load_settings({"PROXY_AUTH": "key"})


def test_unknown_proxy_auth_is_rejected():
    with pytest.raises(ConfigurationError, match="PROXY_AUTH"):
        load_settings({"PROXY_AUTH": "off", "PROXY_KEY": "s3cret"})


def test_max_request_bytes_defaults_and_overrides():
    assert load_settings({"PROXY_KEY": "k"}).max_request_bytes == DEFAULT_MAX_REQUEST_BYTES
    assert load_settings({"PROXY_KEY": "k", "MAX_REQUEST_BYTES": "1024"}).max_request_bytes == 1024


@pytest.mark.parametrize("value", ["lots", "0", "-5"])
def test_bad_max_request_bytes_is_rejected(value):
    with pytest.raises(ConfigurationError, match="MAX_REQUEST_BYTES"):
        load_settings({"PROXY_KEY": "k", "MAX_REQUEST_BYTES": value})


def test_docs_are_off_unless_enabled():
    assert not docs_enabled({})
    assert docs_enabled({"ENABLE_DOCS": "true"})
