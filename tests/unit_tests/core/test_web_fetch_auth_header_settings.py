from __future__ import annotations

import pytest
from pydantic import SecretStr

from core.models import WebFetchAuthHeader
from core.site_settings import SiteSettings, _parse_auth_headers_json


@pytest.fixture
def ss():
    return SiteSettings()


@pytest.fixture(autouse=True)
def clear_docker_secret_cache():
    """Reset the cache so monkeypatched env vars are seen on each test."""
    from core import site_settings as ss_module

    ss_module._docker_secret_cache.clear()
    yield
    ss_module._docker_secret_cache.clear()


def _create_row(domain: str, header_name: str, header_value: str) -> WebFetchAuthHeader:
    row = WebFetchAuthHeader(domain=domain, header_name=header_name)
    row.header_value = header_value
    row.save()
    return row


class TestParseAuthHeadersJson:
    def test_parses_valid_json(self):
        result = _parse_auth_headers_json('{"context7.com": {"X-API-Key": "sk-abc"}}')
        assert "context7.com" in result
        assert result["context7.com"]["X-API-Key"].get_secret_value() == "sk-abc"

    def test_empty_string_returns_empty_dict(self):
        assert _parse_auth_headers_json("") == {}

    def test_invalid_json_returns_empty_dict_and_logs(self, caplog):
        result = _parse_auth_headers_json("not json")
        assert result == {}
        assert any("invalid" in rec.message.lower() for rec in caplog.records)

    def test_wrong_shape_returns_empty_dict(self, caplog):
        assert _parse_auth_headers_json('{"context7.com": "not-a-dict"}') == {}
        assert any("shape" in rec.message.lower() for rec in caplog.records)


@pytest.mark.django_db
class TestWebFetchAuthHeadersProperty:
    def setup_method(self):
        WebFetchAuthHeader.invalidate_cache()

    def test_default_is_empty_dict_when_nothing_set(self, ss):
        assert ss.web_fetch_auth_headers == {}

    def test_db_rows_returned_when_env_unset(self, ss):
        _create_row("context7.com", "X-API-Key", "sk-abc")
        result = ss.web_fetch_auth_headers
        assert isinstance(result["context7.com"]["X-API-Key"], SecretStr)
        assert result["context7.com"]["X-API-Key"].get_secret_value() == "sk-abc"

    def test_env_var_overrides_db(self, ss, monkeypatch):
        _create_row("context7.com", "X-API-Key", "from-db")
        monkeypatch.setenv("DAIV_WEB_FETCH_AUTH_HEADERS", '{"context7.com": {"X-API-Key": "from-env"}}')
        from core import site_settings as ss_module

        ss_module._docker_secret_cache.clear()

        result = ss.web_fetch_auth_headers
        assert result["context7.com"]["X-API-Key"].get_secret_value() == "from-env"

    def test_is_env_locked_for_web_fetch_auth_headers(self, ss, monkeypatch):
        from core import site_settings as ss_module

        ss_module._docker_secret_cache.clear()
        assert ss.is_env_locked("web_fetch_auth_headers") is False

        monkeypatch.setenv("DAIV_WEB_FETCH_AUTH_HEADERS", "{}")
        ss_module._docker_secret_cache.clear()
        assert ss.is_env_locked("web_fetch_auth_headers") is True
