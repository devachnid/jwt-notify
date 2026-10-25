import pytest

from app.config import (
    DEFAULT_BODY_READ_TIMEOUT_SECONDS,
    DEFAULT_MAX_REQUEST_BYTES,
    ConfigurationError,
    docs_enabled,
    load_settings,
)

API_KEY = (
    "my_test_key-26785a09-ab16-4eb0-8407-a37497a57506-3d844edf-8d35-48ac-975b-e847b4f122b0"
)

AUD = "4714c1358e65fe4b408ad6d432a5f878f08194bdb4752441fd56faefa9b2b6f2"
ACCESS = {
    "PROXY_AUTH": "cloudflare-access",
    "CF_ACCESS_TEAM_DOMAIN": "myteam.cloudflareaccess.com",
    "CF_ACCESS_AUD": AUD,
}


def test_refuses_to_start_without_a_proxy_key():
    with pytest.raises(ConfigurationError, match="PROXY_AUTH=cloudflare-access"):
        load_settings({"NOTIFY_API_KEY": API_KEY})


def test_refuses_to_start_without_a_proxy_key_in_token_only_mode():
    with pytest.raises(ConfigurationError):
        load_settings({})


def test_proxy_key_is_used():
    settings = load_settings({"NOTIFY_API_KEY": API_KEY, "PROXY_KEY": "s3cret"})
    assert settings.proxy_key == "s3cret"


@pytest.mark.parametrize("value", ["none", "NONE", " none "])
def test_proxy_auth_none_is_no_longer_accepted(value):
    # An origin that trusts whoever reaches it can be bypassed by anything that
    # gets to the port without passing Access, so the open mode is gone.
    with pytest.raises(ConfigurationError, match="cloudflare-access"):
        load_settings({"NOTIFY_API_KEY": API_KEY, "PROXY_AUTH": value})


def test_cloudflare_access_mode_needs_no_proxy_key():
    settings = load_settings({"NOTIFY_API_KEY": API_KEY, **ACCESS})
    assert settings.proxy_key is None
    assert settings.cloudflare_access is not None
    assert settings.cloudflare_access.aud == AUD
    assert settings.cloudflare_access.issuer == "https://myteam.cloudflareaccess.com"
    assert settings.cloudflare_access.certs_url == (
        "https://myteam.cloudflareaccess.com/cdn-cgi/access/certs"
    )


def test_key_mode_has_no_cloudflare_access():
    assert load_settings({"PROXY_KEY": "s3cret"}).cloudflare_access is None


@pytest.mark.parametrize(
    "domain",
    [
        "myteam",
        "myteam.cloudflareaccess.com",
        "https://myteam.cloudflareaccess.com",
        " https://MyTeam.cloudflareaccess.com/ ",
    ],
)
def test_team_domain_is_normalised(domain):
    settings = load_settings({**ACCESS, "CF_ACCESS_TEAM_DOMAIN": domain})
    assert settings.cloudflare_access.issuer == "https://myteam.cloudflareaccess.com"


@pytest.mark.parametrize(
    "domain", ["", "my team", "myteam.example.com/evil", "http://myteam.cloudflareaccess.com"]
)
def test_bad_team_domain_is_rejected(domain):
    with pytest.raises(ConfigurationError, match="CF_ACCESS_TEAM_DOMAIN"):
        load_settings({**ACCESS, "CF_ACCESS_TEAM_DOMAIN": domain})


def test_cloudflare_access_needs_an_audience():
    with pytest.raises(ConfigurationError, match="CF_ACCESS_AUD"):
        load_settings({**ACCESS, "CF_ACCESS_AUD": " "})


def test_cloudflare_access_conflicts_with_a_proxy_key():
    with pytest.raises(ConfigurationError, match="both set"):
        load_settings({**ACCESS, "PROXY_KEY": "s3cret"})


def test_body_read_timeout_defaults_and_overrides():
    assert load_settings({"PROXY_KEY": "k"}).body_read_timeout == DEFAULT_BODY_READ_TIMEOUT_SECONDS
    settings = load_settings({"PROXY_KEY": "k", "BODY_READ_TIMEOUT_SECONDS": "2.5"})
    assert settings.body_read_timeout == 2.5


@pytest.mark.parametrize("value", ["soon", "0", "-1"])
def test_bad_body_read_timeout_is_rejected(value):
    with pytest.raises(ConfigurationError, match="BODY_READ_TIMEOUT_SECONDS"):
        load_settings({"PROXY_KEY": "k", "BODY_READ_TIMEOUT_SECONDS": value})


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
