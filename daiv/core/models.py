# ruff: noqa: DJ001 — null=True on string fields is intentional; NULL means "use env/default".
from __future__ import annotations

import asyncio
import concurrent.futures
import fnmatch
import logging
from dataclasses import dataclass, field
from typing import Any, ClassVar

from django.core.cache import cache
from django.db import models
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger("daiv.core")

SITE_CONFIGURATION_CACHE_KEY = "site_configuration"
SITE_CONFIGURATION_CACHE_TIMEOUT = 60 * 5  # 5 minutes


@dataclass
class FieldGroup:
    """Definition of a configuration field group for template rendering."""

    key: str
    title: str
    match: list[str] = field(default_factory=list)
    icon: str = ""
    fields: list[str] = field(default_factory=list)


class ThinkingLevelChoices(models.TextChoices):
    MINIMAL = "minimal", _("Minimal")
    LOW = "low", _("Low")
    MEDIUM = "medium", _("Medium")
    HIGH = "high", _("High")


class WebSearchEngineChoices(models.TextChoices):
    DUCKDUCKGO = "duckduckgo", _("DuckDuckGo")
    TAVILY = "tavily", _("Tavily")


class SingletonManager(models.Manager["SiteConfiguration"]):
    """
    Manager that ensures only one row (pk=1) exists.
    """

    def get_instance(self) -> SiteConfiguration:
        instance, _ = self.get_or_create(pk=1)
        return instance


class EncryptedFieldDescriptor:
    """
    Descriptor that transparently encrypts on set and decrypts on get
    for fields storing Fernet-encrypted values in the database.
    """

    def __init__(self, field_name: str):
        self.field_name = field_name
        self.db_column = f"_{field_name}_encrypted"

    def __set_name__(self, owner: type, name: str):
        self.attr_name = name

    def __get__(self, obj: SiteConfiguration | None, objtype: type | None = None) -> str | None:
        if obj is None:
            return self  # type: ignore[return-value]
        raw = getattr(obj, self.db_column, None)
        if raw is None:
            return None
        from core.encryption import decrypt_value

        try:
            return decrypt_value(raw)
        except Exception:
            logger.warning(
                "Failed to decrypt field %s (possible key rotation or data corruption)", self.field_name, exc_info=True
            )
            return None

    def __set__(self, obj: SiteConfiguration, value: str | None):
        if value is None or value == "":
            setattr(obj, self.db_column, None)
            return
        from core.encryption import encrypt_value

        setattr(obj, self.db_column, encrypt_value(value))


class SiteConfiguration(models.Model):
    """
    Singleton model storing site-wide configuration that can be managed through the UI.

    All fields are nullable — ``NULL`` means "use the environment variable or Pydantic default".
    """

    # -- Agent Models --
    agent_model_name = models.CharField(
        _("agent model"), max_length=255, blank=True, null=True, help_text=_("Primary model for agent tasks.")
    )
    agent_fallback_model_name = models.CharField(
        _("agent fallback model"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Fallback model when the primary model fails."),
    )
    agent_thinking_level = models.CharField(
        _("agent thinking level"),
        max_length=10,
        blank=True,
        null=True,
        choices=ThinkingLevelChoices.choices,
        help_text=_("Extended thinking depth for agent tasks. Leave empty to disable thinking."),
    )
    agent_max_model_name = models.CharField(
        _("max model"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Model for tasks when the daiv-max label is present."),
    )
    agent_max_thinking_level = models.CharField(
        _("max thinking level"),
        max_length=10,
        blank=True,
        null=True,
        choices=ThinkingLevelChoices.choices,
        help_text=_("Thinking depth for daiv-max tasks. Leave empty to disable thinking."),
    )
    agent_explore_model_name = models.CharField(
        _("explore model"), max_length=255, blank=True, null=True, help_text=_("Fast model for the explore subagent.")
    )
    agent_recursion_limit = models.PositiveIntegerField(
        _("recursion limit"), blank=True, null=True, help_text=_("Maximum recursion depth for agent loops.")
    )

    # -- Diff to Metadata --
    diff_to_metadata_model_name = models.CharField(
        _("diff-to-metadata model"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Model for generating commit messages and PR descriptions from diffs."),
    )
    diff_to_metadata_fallback_model_name = models.CharField(
        _("diff-to-metadata fallback model"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Fallback model for diff-to-metadata when the primary fails."),
    )

    # -- Web Search --
    web_search_enabled = models.BooleanField(
        _("web search enabled"), null=True, help_text=_("Enable or disable the web search tool.")
    )
    web_search_max_results = models.PositiveIntegerField(
        _("web search max results"),
        blank=True,
        null=True,
        help_text=_("Maximum number of results returned from web search."),
    )
    web_search_engine = models.CharField(
        _("web search engine"),
        max_length=20,
        blank=True,
        null=True,
        choices=WebSearchEngineChoices.choices,
        help_text=_("Search engine to use. Tavily requires an API key."),
    )

    # -- Web Fetch --
    web_fetch_enabled = models.BooleanField(
        _("web fetch enabled"), null=True, help_text=_("Enable or disable the web fetch tool.")
    )
    web_fetch_model_name = models.CharField(
        _("web fetch model"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Model used to process fetched page content."),
    )
    web_fetch_cache_ttl_seconds = models.PositiveIntegerField(
        _("web fetch cache TTL"),
        blank=True,
        null=True,
        help_text=_("Cache time-to-live for fetched pages, in seconds."),
    )
    web_fetch_timeout_seconds = models.PositiveIntegerField(
        _("web fetch timeout"), blank=True, null=True, help_text=_("HTTP timeout for web fetch requests, in seconds.")
    )
    web_fetch_max_content_chars = models.PositiveIntegerField(
        _("web fetch max content"),
        blank=True,
        null=True,
        help_text=_("Maximum page content size (in characters) to analyze in one pass."),
    )

    # -- Sandbox --
    sandbox_timeout = models.FloatField(
        _("sandbox timeout"), blank=True, null=True, help_text=_("Timeout for sandbox requests, in seconds.")
    )
    sandbox_base_image = models.CharField(
        _("sandbox base image"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Default Docker base image for sandbox sessions."),
    )
    sandbox_ephemeral = models.BooleanField(
        _("sandbox ephemeral"), null=True, help_text=_("Whether sandbox sessions are ephemeral by default.")
    )
    sandbox_network_enabled = models.BooleanField(
        _("sandbox network enabled"),
        null=True,
        help_text=_("Whether to enable network access in sandbox sessions by default."),
    )
    sandbox_cpu = models.FloatField(
        _("sandbox CPU"),
        blank=True,
        null=True,
        help_text=_("CPUs to allocate to sandbox sessions by default. Leave empty for no limit."),
    )
    sandbox_memory = models.BigIntegerField(
        _("sandbox memory"),
        blank=True,
        null=True,
        help_text=_("Memory limit in bytes to allocate to sandbox sessions by default. Leave empty for no limit."),
    )

    # -- Features --
    suggest_context_file_enabled = models.BooleanField(
        _("suggest context file"),
        null=True,
        help_text=_("Suggest creating a context file (e.g. AGENTS.md) on new merge requests."),
    )

    # -- Rate Limiting --
    jobs_throttle_rate = models.CharField(
        _("jobs throttle rate"),
        max_length=50,
        blank=True,
        null=True,
        help_text=_("Rate limit for job submissions per authenticated user (e.g. '20/hour')."),
    )

    # -- Providers --
    openrouter_api_base = models.CharField(
        _("OpenRouter API base URL"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Base URL for the OpenRouter API."),
    )

    # -- API Keys / Secrets (encrypted at rest) --
    _anthropic_api_key_encrypted = models.TextField(blank=True, null=True, editable=False)
    _openai_api_key_encrypted = models.TextField(blank=True, null=True, editable=False)
    _google_api_key_encrypted = models.TextField(blank=True, null=True, editable=False)
    _openrouter_api_key_encrypted = models.TextField(blank=True, null=True, editable=False)
    _web_search_api_key_encrypted = models.TextField(blank=True, null=True, editable=False)
    _sandbox_api_key_encrypted = models.TextField(blank=True, null=True, editable=False)

    # Descriptors for transparent encrypt/decrypt
    anthropic_api_key = EncryptedFieldDescriptor("anthropic_api_key")
    openai_api_key = EncryptedFieldDescriptor("openai_api_key")
    google_api_key = EncryptedFieldDescriptor("google_api_key")
    openrouter_api_key = EncryptedFieldDescriptor("openrouter_api_key")
    web_search_api_key = EncryptedFieldDescriptor("web_search_api_key")
    sandbox_api_key = EncryptedFieldDescriptor("sandbox_api_key")

    ENCRYPTED_FIELDS: ClassVar[tuple[str, ...]] = (
        "anthropic_api_key",
        "openai_api_key",
        "google_api_key",
        "openrouter_api_key",
        "web_search_api_key",
        "sandbox_api_key",
    )

    FIELD_GROUPS: ClassVar[list[FieldGroup]] = [
        FieldGroup(
            key="agent",
            title=_("Agent"),
            match=["agent_*", "suggest_context_file_enabled"],
            icon="core/img/icons/agent.svg",
        ),
        FieldGroup(
            key="diff_to_metadata",
            title=_("Diff to Metadata"),
            match=["diff_to_metadata_*"],
            icon="core/img/icons/diff_to_metadata.svg",
        ),
        FieldGroup(
            key="providers",
            title=_("Providers"),
            match=["anthropic_*", "openai_*", "google_*", "openrouter_*"],
            icon="core/img/icons/providers.svg",
        ),
        FieldGroup(
            key="web_search", title=_("Web Search"), match=["web_search_*"], icon="core/img/icons/web_search.svg"
        ),
        FieldGroup(key="web_fetch", title=_("Web Fetch"), match=["web_fetch_*"], icon="core/img/icons/web_fetch.svg"),
        FieldGroup(key="sandbox", title=_("Sandbox"), match=["sandbox_*"], icon="core/img/icons/sandbox.svg"),
        FieldGroup(key="jobs", title=_("Jobs"), match=["jobs_*"], icon="core/img/icons/jobs.svg"),
    ]

    objects: SingletonManager = SingletonManager()

    class Meta:
        verbose_name = _("site configuration")
        verbose_name_plural = _("site configuration")

    def __str__(self) -> str:
        return "Site Configuration"

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.pk = 1
        super().save(*args, **kwargs)
        self._invalidate_cache()

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        # Prevent deletion of the singleton
        raise RuntimeError("SiteConfiguration cannot be deleted.")

    def get_secret_hint(self, field_name: str) -> str | None:
        """
        Return a masked hint for an encrypted field, or ``None`` if not set.

        Args:
            field_name: One of the ``ENCRYPTED_FIELDS`` names (e.g. ``"anthropic_api_key"``).
        """
        value = getattr(self, field_name, None)
        if value is None:
            return None
        from core.encryption import mask_secret

        return mask_secret(value)

    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    @classmethod
    def _fetch_from_cache_or_db(cls) -> SiteConfiguration | None:
        """
        Check the cache, then fall back to the database.

        This method is safe to call from any thread — it manages DB
        connections via ``close_old_connections`` (important when called
        from a ``ThreadPoolExecutor`` thread, which Django does not
        manage automatically).
        """
        cached = cache.get(SITE_CONFIGURATION_CACHE_KEY)
        if cached is not None:
            return cached

        from django.db import close_old_connections

        close_old_connections()
        try:
            instance = cls.objects.get_instance()
        except Exception:
            logger.warning("SiteConfiguration not available; falling back to defaults", exc_info=True)
            return None
        finally:
            close_old_connections()

        cache.set(SITE_CONFIGURATION_CACHE_KEY, instance, SITE_CONFIGURATION_CACHE_TIMEOUT)
        return instance

    @classmethod
    def get_cached(cls) -> SiteConfiguration | None:
        """
        Return the singleton from cache, falling back to the database.

        Works transparently in both sync and async contexts: when called
        from an async event loop, the entire lookup runs in a separate
        thread via a ``ThreadPoolExecutor`` to avoid
        ``SynchronousOnlyOperation``.

        Returns ``None`` if the database is not yet available (e.g. during
        early startup or Django system checks).
        """
        # Detect async context — synchronous ORM calls are forbidden there.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return cls._fetch_from_cache_or_db()

        # Run the full lookup (cache check + DB fallback) in a separate
        # thread so neither the cache nor the ORM call violates Django's
        # async safety check.
        try:
            return cls._executor.submit(cls._fetch_from_cache_or_db).result(timeout=5)
        except Exception:
            logger.warning("SiteConfiguration not available (async context); falling back to defaults", exc_info=True)
            return None

    @classmethod
    def get_field_groups(cls) -> list[FieldGroup]:
        """
        Return field groups with their ``fields`` lists resolved from
        model field names and :attr:`ENCRYPTED_FIELDS`.
        """
        # Collect all candidate field names
        all_fields = [
            f.name for f in cls._meta.get_fields() if f.name != "id" and not f.name.startswith("_") and f.concrete
        ]
        all_fields.extend(cls.ENCRYPTED_FIELDS)

        assigned: set[str] = set()
        groups: list[FieldGroup] = []
        for group_def in cls.FIELD_GROUPS:
            group_fields: list[str] = []
            for field_name in all_fields:
                if field_name in assigned:
                    continue
                if any(fnmatch.fnmatch(field_name, pattern) for pattern in group_def.match):
                    group_fields.append(field_name)
                    assigned.add(field_name)
            groups.append(
                FieldGroup(
                    key=group_def.key,
                    title=group_def.title,
                    match=group_def.match,
                    icon=group_def.icon,
                    fields=group_fields,
                )
            )
        return groups

    @staticmethod
    def _invalidate_cache() -> None:
        cache.delete(SITE_CONFIGURATION_CACHE_KEY)
        logger.info("Invalidated site configuration cache")
