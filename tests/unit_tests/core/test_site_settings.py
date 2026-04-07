from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr

from core.models import SiteConfiguration
from core.site_settings import SiteSettings


@pytest.fixture(autouse=True)
def _clear_docker_secret_cache():
    from core.site_settings import _docker_secret_cache

    _docker_secret_cache.clear()
    yield
    _docker_secret_cache.clear()


@pytest.fixture
def ss():
    return SiteSettings()


class TestDefaults:
    def test_returns_default_when_no_env_or_db(self, ss):
        with patch.object(SiteConfiguration, "get_cached", return_value=MagicMock(agent_recursion_limit=None)):
            assert ss.agent_recursion_limit == 500

    def test_returns_default_for_boolean(self, ss):
        with patch.object(SiteConfiguration, "get_cached", return_value=MagicMock(web_search_enabled=None)):
            assert ss.web_search_enabled is True

    def test_returns_default_for_string(self, ss):
        with patch.object(SiteConfiguration, "get_cached", return_value=MagicMock(web_search_engine=None)):
            assert ss.web_search_engine == "duckduckgo"


class TestDbOverride:
    def test_db_value_overrides_default(self, ss):
        with patch.object(SiteConfiguration, "get_cached", return_value=MagicMock(agent_recursion_limit=200)):
            assert ss.agent_recursion_limit == 200

    def test_db_none_falls_to_default(self, ss):
        with patch.object(SiteConfiguration, "get_cached", return_value=MagicMock(agent_recursion_limit=None)):
            assert ss.agent_recursion_limit == 500

    def test_db_empty_string_falls_to_default(self, ss):
        with patch.object(SiteConfiguration, "get_cached", return_value=MagicMock(agent_model_name="")):
            assert ss.agent_model_name == "openrouter:anthropic/claude-sonnet-4.6"


class TestEnvOverride:
    def test_env_overrides_db_and_default(self, ss, monkeypatch):
        monkeypatch.setenv("DAIV_AGENT_RECURSION_LIMIT", "999")
        with patch.object(SiteConfiguration, "get_cached", return_value=MagicMock(agent_recursion_limit=200)):
            assert ss.agent_recursion_limit == 999

    def test_env_override_bool(self, ss, monkeypatch):
        monkeypatch.setenv("DAIV_WEB_SEARCH_ENABLED", "false")
        with patch.object(SiteConfiguration, "get_cached", return_value=MagicMock(web_search_enabled=True)):
            assert ss.web_search_enabled is False

    def test_env_override_float(self, ss, monkeypatch):
        monkeypatch.setenv("DAIV_SANDBOX_TIMEOUT", "30.5")
        with patch.object(SiteConfiguration, "get_cached", return_value=MagicMock(sandbox_timeout=600)):
            assert ss.sandbox_timeout == 30.5

    def test_api_key_env_override_uses_custom_name(self, ss, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-env")
        mock_config = MagicMock()
        mock_config.anthropic_api_key = "sk-from-db"
        with patch.object(SiteConfiguration, "get_cached", return_value=mock_config):
            result = ss.anthropic_api_key
            assert isinstance(result, SecretStr)
            assert result.get_secret_value() == "sk-test-env"


class TestEnvLocked:
    def test_is_env_locked_true(self, ss, monkeypatch):
        monkeypatch.setenv("DAIV_AGENT_MODEL_NAME", "x")
        assert ss.is_env_locked("agent_model_name") is True

    def test_is_env_locked_false(self, ss):
        assert ss.is_env_locked("agent_model_name") is False

    def test_is_env_locked_api_key_custom_name(self, ss, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "x")
        assert ss.is_env_locked("openai_api_key") is True


class TestGetEnvVarName:
    def test_default_convention(self, ss):
        assert ss.get_env_var_name("agent_model_name") == "DAIV_AGENT_MODEL_NAME"

    def test_api_key_override(self, ss):
        assert ss.get_env_var_name("anthropic_api_key") == "ANTHROPIC_API_KEY"


class TestGetDefaults:
    def test_returns_string_dict(self, ss):
        defaults = ss.get_defaults()
        assert isinstance(defaults, dict)
        assert defaults["agent_recursion_limit"] == "500"
        assert defaults["web_search_enabled"] == "True"


class TestDbUnavailable:
    def test_falls_back_to_default_when_db_unavailable(self, ss):
        with patch.object(SiteConfiguration, "get_cached", return_value=None):
            assert ss.agent_recursion_limit == 500

    def test_secret_returns_none_when_db_unavailable(self, ss, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with patch.object(SiteConfiguration, "get_cached", return_value=None):
            assert ss.anthropic_api_key is None


class TestDockerSecretOverride:
    def test_docker_secret_overrides_db_and_default(self, ss):
        mock_config = MagicMock()
        mock_config.anthropic_api_key = "sk-from-db"
        mock_secret = patch("core.site_settings.get_docker_secret", return_value="sk-from-docker-secret")
        with mock_secret as mock_fn, patch.object(SiteConfiguration, "get_cached", return_value=mock_config):
            result = ss.anthropic_api_key
            assert isinstance(result, SecretStr)
            assert result.get_secret_value() == "sk-from-docker-secret"
            mock_fn.assert_called_with("ANTHROPIC_API_KEY", default=None)

    def test_docker_secret_locks_field(self, ss):
        with patch("core.site_settings.get_docker_secret", return_value="sk-from-docker-secret") as mock_fn:
            assert ss.is_env_locked("anthropic_api_key") is True
            mock_fn.assert_called_with("ANTHROPIC_API_KEY", default=None)

    def test_no_docker_secret_falls_through(self, ss):
        with (
            patch("core.site_settings.get_docker_secret", return_value=None),
            patch.object(SiteConfiguration, "get_cached", return_value=MagicMock(agent_recursion_limit=200)),
        ):
            assert ss.agent_recursion_limit == 200


class TestEnvVarConventionForSecrets:
    def test_sandbox_api_key_uses_convention(self, ss, monkeypatch):
        monkeypatch.setenv("DAIV_SANDBOX_API_KEY", "sk-sandbox-test")
        mock_config = MagicMock()
        mock_config.sandbox_api_key = None
        with patch.object(SiteConfiguration, "get_cached", return_value=mock_config):
            result = ss.sandbox_api_key
            assert isinstance(result, SecretStr)
            assert result.get_secret_value() == "sk-sandbox-test"

    def test_web_search_api_key_uses_convention(self, ss, monkeypatch):
        monkeypatch.setenv("DAIV_WEB_SEARCH_API_KEY", "sk-search-test")
        mock_config = MagicMock()
        mock_config.web_search_api_key = None
        with patch.object(SiteConfiguration, "get_cached", return_value=mock_config):
            result = ss.web_search_api_key
            assert isinstance(result, SecretStr)
            assert result.get_secret_value() == "sk-search-test"


class TestParseEnvValueErrors:
    def test_invalid_int_raises_value_error(self, ss, monkeypatch):
        monkeypatch.setenv("DAIV_AGENT_RECURSION_LIMIT", "abc")
        with (
            patch.object(SiteConfiguration, "get_cached", return_value=MagicMock(agent_recursion_limit=None)),
            pytest.raises(ValueError, match="Cannot parse"),
        ):
            ss.agent_recursion_limit  # noqa: B018


class TestUnknownField:
    def test_raises_attribute_error(self, ss):
        with pytest.raises(AttributeError, match="no field"):
            ss.nonexistent_field  # noqa: B018
