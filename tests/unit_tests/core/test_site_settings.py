from dataclasses import asdict
from unittest.mock import MagicMock, patch

from django.db import models

import pytest
from pydantic import SecretStr

from core.models import SiteConfiguration
from core.site_settings import SiteSettings


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

    def test_env_override_small_int(self, ss, monkeypatch):
        monkeypatch.setenv("DAIV_PIPELINE_WATCH_MAX_ATTEMPTS", "2")
        with patch.object(SiteConfiguration, "get_cached", return_value=MagicMock(pipeline_watch_max_attempts=5)):
            assert ss.pipeline_watch_max_attempts == 2

    def test_every_configurable_int_field_coerces_from_the_environment(self, ss, monkeypatch):
        """An uncoerced field yields a ``str``, and the arithmetic downstream raises ``TypeError``
        rather than misbehaving visibly, so pin the whole family rather than one member."""
        from django.db import models

        for name in ss.FIELD_DEFAULTS:
            field = SiteConfiguration._meta.get_field(name)
            if not isinstance(field, models.IntegerField) or isinstance(field, models.BooleanField):
                continue
            monkeypatch.setenv(ss.get_env_var_name(name), "7")
            with patch.object(SiteConfiguration, "get_cached", return_value=None):
                assert getattr(ss, name) == 7, f"{name} ({type(field).__name__}) was not coerced"

    def test_api_key_env_override_uses_custom_name(self, ss, monkeypatch):
        monkeypatch.setenv("ALLAUTH_CLIENT_SECRET", "sk-test-env")
        mock_config = MagicMock()
        mock_config.auth_client_secret = "sk-from-db"  # noqa: S105
        with patch.object(SiteConfiguration, "get_cached", return_value=mock_config):
            result = ss.auth_client_secret
            assert isinstance(result, SecretStr)
            assert result.get_secret_value() == "sk-test-env"


@pytest.mark.parametrize("field", ["agent_thinking_level", "agent_max_thinking_level"])
class TestEmptyThinkingLevel:
    """The help text on these fields says an empty value disables thinking."""

    def test_d7_an_empty_value_in_the_database_falls_back_to_the_default(self, ss, field):
        with patch.object(SiteConfiguration, "get_cached", return_value=MagicMock(**{field: ""})):
            assert getattr(ss, field) == ss.FIELD_DEFAULTS[field]

    def test_d7_an_empty_value_in_the_environment_disables_thinking(self, ss, field, monkeypatch):
        monkeypatch.setenv(ss.get_env_var_name(field), "")
        with patch.object(SiteConfiguration, "get_cached", return_value=MagicMock(**{field: "high"})):
            assert getattr(ss, field) == ""


class TestEnvLocked:
    def test_is_env_locked_true(self, ss, monkeypatch):
        monkeypatch.setenv("DAIV_AGENT_MODEL_NAME", "x")
        assert ss.is_env_locked("agent_model_name") is True

    def test_is_env_locked_false(self, ss):
        assert ss.is_env_locked("agent_model_name") is False

    def test_is_env_locked_api_key_custom_name(self, ss, monkeypatch):
        monkeypatch.setenv("ALLAUTH_CLIENT_SECRET", "x")
        assert ss.is_env_locked("auth_client_secret") is True


class TestGetEnvVarName:
    def test_default_convention(self, ss):
        assert ss.get_env_var_name("agent_model_name") == "DAIV_AGENT_MODEL_NAME"

    def test_api_key_override(self, ss):
        assert ss.get_env_var_name("auth_client_secret") == "ALLAUTH_CLIENT_SECRET"


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
        monkeypatch.delenv("DAIV_WEB_SEARCH_API_KEY", raising=False)
        with patch.object(SiteConfiguration, "get_cached", return_value=None):
            assert ss.web_search_api_key is None


class TestDockerSecretOverride:
    def test_docker_secret_overrides_db_and_default(self, ss):
        mock_config = MagicMock()
        mock_config.auth_client_secret = "sk-from-db"  # noqa: S105
        mock_secret = patch("core.site_settings.get_docker_secret", return_value="sk-from-docker-secret")
        with mock_secret as mock_fn, patch.object(SiteConfiguration, "get_cached", return_value=mock_config):
            result = ss.auth_client_secret
            assert isinstance(result, SecretStr)
            assert result.get_secret_value() == "sk-from-docker-secret"
            mock_fn.assert_called_with("ALLAUTH_CLIENT_SECRET", default=None)

    def test_docker_secret_locks_field(self, ss):
        with patch("core.site_settings.get_docker_secret", return_value="sk-from-docker-secret") as mock_fn:
            assert ss.is_env_locked("auth_client_secret") is True
            mock_fn.assert_called_with("ALLAUTH_CLIENT_SECRET", default=None)

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


class TestMemoryDefaults:
    def test_memory_enabled_defaults_true(self, ss):
        with patch.object(SiteConfiguration, "get_cached", return_value=MagicMock(memory_enabled=None)):
            assert ss.memory_enabled is True

    def test_extraction_model_default(self, ss):
        with patch.object(SiteConfiguration, "get_cached", return_value=MagicMock(memory_extraction_model_name=None)):
            assert ss.memory_extraction_model_name == "openrouter:openai/gpt-5.4-mini"

    def test_extraction_fallback_model_default(self, ss):
        with patch.object(
            SiteConfiguration, "get_cached", return_value=MagicMock(memory_extraction_fallback_model_name=None)
        ):
            assert ss.memory_extraction_fallback_model_name == "openrouter:anthropic/claude-haiku-4.5"

    def test_consolidation_model_default_is_empty(self, ss):
        # Optional override: empty means "reuse the repository's agent model".
        with patch.object(
            SiteConfiguration, "get_cached", return_value=MagicMock(memory_consolidation_model_name=None)
        ):
            assert ss.memory_consolidation_model_name is None

    def test_threshold_and_budget_defaults(self, ss):
        cfg = MagicMock(
            memory_consolidation_min_pending=None,
            memory_consolidation_min_interval_hours=None,
            memory_max_lines=None,
            memory_max_bytes=None,
        )
        with patch.object(SiteConfiguration, "get_cached", return_value=cfg):
            assert ss.memory_consolidation_min_pending == 10
            assert ss.memory_consolidation_min_interval_hours == 24
            assert ss.memory_max_lines == 200
            assert ss.memory_max_bytes == 10_240

    def test_memory_enabled_env_override(self, ss, monkeypatch):
        monkeypatch.setenv("DAIV_MEMORY_ENABLED", "false")
        with patch.object(SiteConfiguration, "get_cached", return_value=MagicMock(memory_enabled=True)):
            assert ss.memory_enabled is False

    def test_max_lines_db_override(self, ss):
        with patch.object(SiteConfiguration, "get_cached", return_value=MagicMock(memory_max_lines=50)):
            assert ss.memory_max_lines == 50


def _value_from(source: str, name: str, default):
    """A value unlike ``name``'s default, as the database stores it or the environment spells it."""
    field = SiteConfiguration._meta.get_field(name)
    if isinstance(field, models.BooleanField):
        value = not default
    elif isinstance(field, models.IntegerField):
        value = 4242
    elif isinstance(field, models.FloatField):
        value = 42.5
    else:
        value = f"{source}-{name}"
    if source == "environment":
        return str(value).lower()
    return value


class TestSnapshot:
    @pytest.mark.parametrize("source", ["default", "database", "environment"])
    def test_agrees_with_attribute_access_from_one_configuration_read(self, ss, monkeypatch, source):
        db_values = {}
        for name, default in ss.FIELD_DEFAULTS.items():
            if source == "database":
                db_values[name] = _value_from(source, name, default)
            elif source == "environment":
                monkeypatch.setenv(ss.get_env_var_name(name), _value_from(source, name, default))

        with patch.object(SiteConfiguration, "get_cached", return_value=SiteConfiguration(**db_values)) as get_cached:
            snapshot = ss.snapshot()
            assert get_cached.call_count == 1
            resolved = {name: getattr(ss, name) for name in ss.FIELD_DEFAULTS}

        assert asdict(snapshot) == resolved
        if source != "default":
            assert all(resolved[name] != default for name, default in ss.FIELD_DEFAULTS.items())

    def test_a_malformed_env_var_fails_only_reads_of_its_field(self, ss, monkeypatch):
        monkeypatch.setenv("DAIV_WEB_FETCH_TIMEOUT_SECONDS", "15s")

        with patch.object(SiteConfiguration, "get_cached", return_value=SiteConfiguration()):
            snapshot = ss.snapshot()
            with pytest.raises(ValueError, match="Cannot parse") as from_attribute_access:
                ss.web_fetch_timeout_seconds  # noqa: B018

        for _ in range(2):
            with pytest.raises(ValueError, match="Cannot parse") as from_snapshot:
                snapshot.web_fetch_timeout_seconds  # noqa: B018
            assert type(from_snapshot.value) is type(from_attribute_access.value)
        others = {name: getattr(snapshot, name) for name in ss.FIELD_DEFAULTS if name != "web_fetch_timeout_seconds"}
        assert others == {name: value for name, value in ss.FIELD_DEFAULTS.items() if name in others}
