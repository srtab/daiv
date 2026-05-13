from __future__ import annotations

import pytest
from pydantic import SecretStr

from core.models import WebFetchAuthHeader
from core.site_settings import SiteSettings, _parse_auth_headers_json


@pytest.fixture
def ss():
    return SiteSettings()


class TestParseAuthHeadersJson:
    def test_parses_valid_json(self):
        result = _parse_auth_headers_json('{"context7.com": {"X-API-Key": "sk-abc"}}')
        assert "context7.com" in result
        assert result["context7.com"]["X-API-Key"].get_secret_value() == "sk-abc"

    def test_lowercases_domain(self):
        result = _parse_auth_headers_json('{"Context7.COM": {"X-API-Key": "sk-abc"}}')
        assert "context7.com" in result
        assert "Context7.COM" not in result

    def test_empty_string_returns_empty_dict(self):
        assert _parse_auth_headers_json("") == {}

    @pytest.mark.parametrize(
        "raw",
        [
            "not json",
            '{"context7.com": "not-a-dict"}',
            '{"context7.com": {"X-Key": 123}}',  # non-string value
            '{"context7.com": {123: "v"}}',  # non-string key (json reject)
        ],
    )
    def test_malformed_returns_empty_dict_and_logs_error(self, raw, caplog):
        import logging

        caplog.set_level(logging.ERROR)
        assert _parse_auth_headers_json(raw) == {}
        assert any("could not be parsed" in rec.message.lower() for rec in caplog.records)


@pytest.mark.django_db
class TestWebFetchAuthHeadersProperty:
    def setup_method(self):
        WebFetchAuthHeader.invalidate_cache()

    def test_default_is_empty_dict_when_nothing_set(self, ss):
        assert ss.web_fetch_auth_headers == {}

    def test_db_rows_returned_when_env_unset(self, ss, make_auth_header):
        make_auth_header("context7.com", "X-API-Key", "sk-abc")
        result = ss.web_fetch_auth_headers
        assert isinstance(result["context7.com"]["X-API-Key"], SecretStr)
        assert result["context7.com"]["X-API-Key"].get_secret_value() == "sk-abc"

    def test_env_var_overrides_db(self, ss, make_auth_header, monkeypatch):
        make_auth_header("context7.com", "X-API-Key", "from-db")
        monkeypatch.setenv("DAIV_WEB_FETCH_AUTH_HEADERS", '{"context7.com": {"X-API-Key": "from-env"}}')

        result = ss.web_fetch_auth_headers
        assert result["context7.com"]["X-API-Key"].get_secret_value() == "from-env"

    def test_is_env_locked_for_web_fetch_auth_headers(self, ss, monkeypatch):
        from core import site_settings as ss_module

        assert ss.is_env_locked("web_fetch_auth_headers") is False

        # Clear after setenv: the False-result above cached None, which would
        # otherwise mask the new env value.
        monkeypatch.setenv("DAIV_WEB_FETCH_AUTH_HEADERS", "{}")
        ss_module._docker_secret_cache.clear()
        assert ss.is_env_locked("web_fetch_auth_headers") is True
