from __future__ import annotations

import functools
import logging
import os
from typing import Any, ClassVar

from pydantic import SecretStr

logger = logging.getLogger("daiv.core")


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
    }


@functools.cache
def _get_field_defaults() -> dict[str, Any]:
    return _build_field_defaults()


class SiteSettings:
    """
    Unified accessor for site-wide configurable settings backed by
    :class:`~core.models.SiteConfiguration`.

    Priority chain (highest to lowest):
        1. Environment variable (hard override — UI shows field as locked)
        2. Database value (non-null — set via the configuration UI)
        3. Field defaults (hardcoded fallback)
    """

    # Env var names that don't follow the ``DAIV_{field.upper()}`` convention.
    ENV_VAR_OVERRIDES: ClassVar[dict[str, str]] = {
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "openai_api_key": "OPENAI_API_KEY",
        "google_api_key": "GOOGLE_API_KEY",
        "openrouter_api_key": "OPENROUTER_API_KEY",
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

        # 1. Environment variable wins
        env_var = self.get_env_var_name(name)
        env_value = os.environ.get(env_var)
        if env_value is not None:
            if name in SiteConfiguration.ENCRYPTED_FIELDS:
                return SecretStr(env_value)
            field = SiteConfiguration._meta.get_field(name)
            return self._parse_env_value(env_value, field)

        # 2. Database value (non-null)
        config = SiteConfiguration.get_cached()
        if config is not None:
            db_value = getattr(config, name, None)
            if db_value is not None:
                if name in SiteConfiguration.ENCRYPTED_FIELDS:
                    return SecretStr(db_value)
                return db_value

        # 3. Hardcoded default
        return field_defaults.get(name)

    def get_env_var_name(self, name: str) -> str:
        """Return the environment variable name for a configurable field."""
        return self.ENV_VAR_OVERRIDES.get(name, f"DAIV_{name.upper()}")

    def is_env_locked(self, name: str) -> bool:
        """Check if a field is locked by an environment variable."""
        return self.get_env_var_name(name) in os.environ

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
