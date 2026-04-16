from __future__ import annotations

import functools
import logging
from typing import Any, ClassVar

from get_docker_secret import get_docker_secret
from pydantic import SecretStr

logger = logging.getLogger("daiv.core")

# Docker secrets are static for the lifetime of the container, so we cache
# lookups to avoid a file-open attempt on every SiteSettings attribute access.
_SENTINEL = object()
_docker_secret_cache: dict[str, str | None] = {}


def _get_docker_secret_cached(name: str) -> str | None:
    """Cached wrapper around ``get_docker_secret`` to avoid repeated file I/O."""
    cached = _docker_secret_cache.get(name, _SENTINEL)
    if cached is not _SENTINEL:
        return cached  # type: ignore[return-value]
    value = get_docker_secret(name, default=None)
    _docker_secret_cache[name] = value
    return value


def _build_field_defaults() -> dict[str, Any]:
    """Build the defaults dict at first access (avoids import-time cross-layer dependency)."""
    from automation.agent.constants import ModelName
    from core.models import ThinkingLevelChoices, WebSearchEngineChoices

    return {
        # Agent
        "agent_model_name": ModelName.CLAUDE_SONNET_4_6,
        "agent_fallback_model_name": ModelName.GPT_5_3_CODEX,
        "agent_thinking_level": ThinkingLevelChoices.MEDIUM,
        "agent_max_model_name": ModelName.CLAUDE_OPUS_4_6,
        "agent_max_thinking_level": ThinkingLevelChoices.HIGH,
        "agent_explore_model_name": ModelName.CLAUDE_HAIKU_4_5,
        "agent_explore_fallback_model_name": ModelName.GPT_5_4_MINI,
        "agent_recursion_limit": 500,
        "suggest_context_file_enabled": True,
        # Diff to Metadata
        "diff_to_metadata_model_name": ModelName.GPT_5_4_MINI,
        "diff_to_metadata_fallback_model_name": ModelName.CLAUDE_HAIKU_4_5,
        # Web Search
        "web_search_enabled": True,
        "web_search_max_results": 5,
        "web_search_engine": WebSearchEngineChoices.DUCKDUCKGO,
        # Web Fetch
        "web_fetch_enabled": True,
        "web_fetch_model_name": ModelName.CLAUDE_HAIKU_4_5,
        "web_fetch_cache_ttl_seconds": 900,
        "web_fetch_timeout_seconds": 15,
        "web_fetch_max_content_chars": 50_000,
        # Providers
        "openrouter_api_base": "https://openrouter.ai/api/v1",
        # Sandbox
        "sandbox_timeout": 600,
        "sandbox_cpu": None,
        "sandbox_memory": None,
        "sandbox_base_image": "python:3.12-bookworm",
        "sandbox_ephemeral": False,
        "sandbox_network_enabled": False,
        # Jobs
        "jobs_throttle_rate": "20/hour",
        # Authentication
        "auth_login_enabled": False,
        "auth_client_id": None,
        "auth_gitlab_url": "https://gitlab.com",
        "auth_gitlab_server_url": None,
    }


@functools.cache
def _get_field_defaults() -> dict[str, Any]:
    return _build_field_defaults()


class SiteSettings:
    """
    Unified accessor for site-wide configurable settings backed by
    :class:`~core.models.SiteConfiguration`.

    Priority chain (highest to lowest):
        1. Docker secret or environment variable (hard override — UI shows field as locked)
        2. Database value (non-null — set via the configuration UI)
        3. Field defaults (hardcoded fallback)
    """

    # Env var names that don't follow the ``DAIV_{field.upper()}`` convention.
    ENV_VAR_OVERRIDES: ClassVar[dict[str, str]] = {
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "openai_api_key": "OPENAI_API_KEY",
        "google_api_key": "GOOGLE_API_KEY",
        "openrouter_api_key": "OPENROUTER_API_KEY",
        "auth_client_id": "ALLAUTH_CLIENT_ID",
        "auth_client_secret": "ALLAUTH_CLIENT_SECRET",
        "auth_gitlab_url": "ALLAUTH_GITLAB_URL",
        "auth_gitlab_server_url": "ALLAUTH_GITLAB_SERVER_URL",
    }

    @property
    def FIELD_DEFAULTS(self) -> dict[str, Any]:  # noqa: N802
        """Effective defaults for every configurable field (lazy-loaded to avoid import-time issues)."""
        return _get_field_defaults()

    def __getattr__(self, name: str) -> Any:
        from core.models import SiteConfiguration

        field_defaults = _get_field_defaults()

        # Only handle known configurable fields
        if name not in field_defaults and name not in SiteConfiguration.ENCRYPTED_FIELDS:
            raise AttributeError(f"SiteSettings has no field '{name}'")

        # 1. Docker secret / environment variable wins
        env_var = self.get_env_var_name(name)
        env_value = _get_docker_secret_cached(env_var)
        if env_value is not None:
            if name in SiteConfiguration.ENCRYPTED_FIELDS:
                return SecretStr(env_value)
            field = SiteConfiguration._meta.get_field(name)
            return self._parse_env_value(env_value, field)

        # 2. Database value (non-null and non-empty-string)
        config = SiteConfiguration.get_cached()
        if config is not None:
            db_value = getattr(config, name, None)
            if db_value is not None and db_value != "":
                if name in SiteConfiguration.ENCRYPTED_FIELDS:
                    return SecretStr(db_value)
                return db_value

        # 3. Hardcoded default
        return field_defaults.get(name)

    def get_env_var_name(self, name: str) -> str:
        """Return the environment variable name for a configurable field."""
        return self.ENV_VAR_OVERRIDES.get(name, f"DAIV_{name.upper()}")

    def is_env_locked(self, name: str) -> bool:
        """Check if a field is locked by an environment variable or Docker secret."""
        return _get_docker_secret_cached(self.get_env_var_name(name)) is not None

    def get_defaults(self) -> dict[str, str]:
        """Return all defaults formatted as strings (for form placeholders)."""
        return {k: str(v) for k, v in _get_field_defaults().items() if v is not None}

    @staticmethod
    def _parse_env_value(value: str, field: Any) -> Any:
        """Coerce a string environment variable to the model field's type."""
        from django.db import models

        try:
            if isinstance(field, models.BooleanField):
                return value.lower() in ("true", "1", "yes", "on")
            if isinstance(field, (models.PositiveIntegerField, models.BigIntegerField)):
                return int(value)
            if isinstance(field, models.FloatField):
                return float(value)
        except (ValueError, TypeError) as e:
            raise ValueError(
                f"Cannot parse env var value '{value}' as {type(field).__name__} for field '{field.name}': {e}"
            ) from e
        return value


site_settings = SiteSettings()
