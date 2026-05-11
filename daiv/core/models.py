# ruff: noqa: DJ001 — null=True on string fields is intentional; NULL means "use env/default".
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import fnmatch
import logging
from dataclasses import dataclass
from typing import Any, ClassVar

from django.core.cache import cache
from django.db import models
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger("daiv.core")

SITE_CONFIGURATION_CACHE_KEY = "site_configuration"
SITE_CONFIGURATION_CACHE_TIMEOUT = 60 * 5  # 5 minutes

WEB_FETCH_AUTH_HEADERS_CACHE_KEY = "web_fetch_auth_headers"
WEB_FETCH_AUTH_HEADERS_CACHE_TIMEOUT = 60 * 5  # 5 minutes

PROVIDERS_CACHE_KEY = "providers"
PROVIDERS_CACHE_TIMEOUT = 60 * 5  # 5 minutes

_UNSET = object()


@dataclass(frozen=True)
class FieldGroup:
    """Definition of a configuration field group for template rendering."""

    key: str
    title: str
    match: tuple[str, ...] = ()
    icon: str = ""
    fields: tuple[str, ...] = ()
    toggle_field: str = ""


class ThinkingLevelChoices(models.TextChoices):
    MINIMAL = "minimal", _("Minimal")
    LOW = "low", _("Low")
    MEDIUM = "medium", _("Medium")
    HIGH = "high", _("High")


class WebSearchEngineChoices(models.TextChoices):
    DUCKDUCKGO = "duckduckgo", _("DuckDuckGo")
    TAVILY = "tavily", _("Tavily")


class ProviderType(models.TextChoices):
    OPENAI = "openai", _("OpenAI")
    ANTHROPIC = "anthropic", _("Anthropic")
    GOOGLE_GENAI = "google_genai", _("Google Gemini")
    OPENROUTER = "openrouter", _("OpenRouter")


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
        from cryptography.fernet import InvalidToken

        from core.encryption import decrypt_value

        try:
            return decrypt_value(raw)
        except InvalidToken:
            logger.exception(
                "Failed to decrypt field '%s': invalid token (key rotation or data corruption). "
                "Re-enter the secret through the configuration UI or check DAIV_ENCRYPTION_KEY.",
                self.field_name,
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
        _("model"), max_length=255, blank=True, null=True, help_text=_("Primary model for agent tasks.")
    )
    agent_fallback_model_name = models.CharField(
        _("fallback model"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Fallback model when the primary model fails."),
    )
    agent_thinking_level = models.CharField(
        _("thinking level"),
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
    agent_explore_fallback_model_name = models.CharField(
        _("explore fallback model"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Fallback model when the explore model fails."),
    )
    agent_recursion_limit = models.PositiveIntegerField(
        _("recursion limit"), blank=True, null=True, help_text=_("Maximum recursion depth for agent loops.")
    )

    # -- Commit & PR Writer --
    diff_to_metadata_model_name = models.CharField(
        _("model"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Model for generating commit messages and PR descriptions from diffs."),
    )
    diff_to_metadata_fallback_model_name = models.CharField(
        _("fallback model"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Fallback model used when the primary fails."),
    )

    # -- Titling --
    titling_model_name = models.CharField(
        _("model"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Model for generating chat thread and activity titles from prompts."),
    )
    titling_fallback_model_name = models.CharField(
        _("fallback model"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Fallback model used when the primary fails."),
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

    # -- Authentication --
    auth_login_enabled = models.BooleanField(
        _("enable OAuth login"),
        null=True,
        help_text=_("Allow users to sign in with their Git platform account (GitHub or GitLab)."),
    )
    auth_signup_open = models.BooleanField(
        _("open social signup"),
        null=True,
        help_text=_(
            "Allow anyone who authenticates via the configured Git platform to create an account."
            " When disabled, only users pre-created by an admin can sign in."
        ),
    )
    auth_client_id = models.CharField(
        _("OAuth client ID"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("OAuth application client ID for the configured Git platform."),
    )
    auth_gitlab_url = models.CharField(
        _("GitLab URL"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Browser-facing URL of your GitLab instance."),
    )
    auth_gitlab_server_url = models.CharField(
        _("GitLab server URL"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_(
            "Server-to-server URL for GitLab API calls (token exchange, profile fetch) in Docker-internal networks."
            " Leave empty to use the GitLab URL."
        ),
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

    # -- Rocket Chat --
    rocketchat_enabled = models.BooleanField(
        _("enable Rocket Chat"), null=True, help_text=_("Offer Rocket Chat as a notification channel for users.")
    )
    rocketchat_url = models.CharField(
        _("Rocket Chat URL"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Base URL of your Rocket Chat instance (e.g. https://rc.example.com)."),
    )
    rocketchat_user_id = models.CharField(
        _("Rocket Chat bot user ID"),
        max_length=64,
        blank=True,
        null=True,
        help_text=_("The bot user's _id, sent as the X-User-Id header."),
    )

    # -- API Keys / Secrets (encrypted at rest) --
    _web_search_api_key_encrypted = models.TextField(blank=True, null=True, editable=False)
    _sandbox_api_key_encrypted = models.TextField(blank=True, null=True, editable=False)
    _auth_client_secret_encrypted = models.TextField(blank=True, null=True, editable=False)
    _rocketchat_auth_token_encrypted = models.TextField(blank=True, null=True, editable=False)

    # Descriptors for transparent encrypt/decrypt
    web_search_api_key = EncryptedFieldDescriptor("web_search_api_key")
    sandbox_api_key = EncryptedFieldDescriptor("sandbox_api_key")
    auth_client_secret = EncryptedFieldDescriptor("auth_client_secret")
    rocketchat_auth_token = EncryptedFieldDescriptor("rocketchat_auth_token")

    MODEL_NAME_FIELDS: ClassVar[tuple[str, ...]] = (
        "agent_model_name",
        "agent_fallback_model_name",
        "agent_max_model_name",
        "agent_explore_model_name",
        "agent_explore_fallback_model_name",
        "diff_to_metadata_model_name",
        "diff_to_metadata_fallback_model_name",
        "titling_model_name",
        "titling_fallback_model_name",
        "web_fetch_model_name",
    )

    ENCRYPTED_FIELDS: ClassVar[tuple[str, ...]] = (
        "web_search_api_key",
        "sandbox_api_key",
        "auth_client_secret",
        "rocketchat_auth_token",
    )

    FIELD_GROUPS: ClassVar[tuple[FieldGroup, ...]] = (
        FieldGroup(key="agent", title=_("Agent"), match=("agent_*", "suggest_context_file_enabled"), icon="agent"),
        FieldGroup(
            key="diff_to_metadata",
            title=_("Commit & PR Writer"),
            match=("diff_to_metadata_*",),
            icon="diff-to-metadata",
        ),
        FieldGroup(key="titling", title=_("Titling"), match=("titling_*",), icon="chat-bubble"),
        FieldGroup(key="providers", title=_("Providers"), match=(), icon="providers"),
        FieldGroup(
            key="web_search",
            title=_("Web Search"),
            match=("web_search_*",),
            icon="web-search",
            toggle_field="web_search_enabled",
        ),
        FieldGroup(
            key="web_fetch",
            title=_("Web Fetch"),
            match=("web_fetch_*",),
            icon="web-fetch",
            toggle_field="web_fetch_enabled",
        ),
        FieldGroup(key="sandbox", title=_("Sandbox"), match=("sandbox_*",), icon="sandbox"),
        FieldGroup(key="jobs", title=_("Jobs"), match=("jobs_*",), icon="jobs"),
        FieldGroup(
            key="rocketchat",
            title=_("Rocket Chat"),
            match=("rocketchat_*",),
            icon="rocketchat",
            toggle_field="rocketchat_enabled",
        ),
        FieldGroup(
            key="authentication",
            title=_("Authentication"),
            match=("auth_*",),
            icon="lock-closed",
            toggle_field="auth_login_enabled",
        ),
    )

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

    # Multiple workers so concurrent async callers don't queue behind a single slow
    # DB/cache call and timeout.  The result is cached, so most calls resolve quickly;
    # extra threads are only needed when the cache is cold or the DB is slow.
    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

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

        try:
            close_old_connections()
            instance = cls.objects.get_instance()
        except Exception:  # noqa: BLE001 — DB/cache may fail during startup or degraded state
            logger.exception("SiteConfiguration not available; falling back to defaults")
            return None
        finally:
            with contextlib.suppress(Exception):
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
        except concurrent.futures.TimeoutError:
            logger.error("SiteConfiguration lookup timed out (5s) in async context; falling back to defaults")
            return None
        except Exception:  # noqa: BLE001
            logger.exception("SiteConfiguration not available (async context); falling back to defaults")
            return None

    @classmethod
    def get_field_groups(cls) -> list[FieldGroup]:
        """
        Return field groups with their ``fields`` lists resolved from
        model field names and :attr:`ENCRYPTED_FIELDS`.
        """
        # Collect all candidate field names, interleaving encrypted fields
        # next to sibling model fields so related fields stay together.
        all_fields = [
            f.name for f in cls._meta.get_fields() if f.name != "id" and not f.name.startswith("_") and f.concrete
        ]
        for enc_field in cls.ENCRYPTED_FIELDS:
            prefix = enc_field.rsplit("_", 2)[0]
            insert_at = len(all_fields)
            for i, name in enumerate(all_fields):
                if name.startswith(f"{prefix}_"):
                    insert_at = i + 1
            all_fields.insert(insert_at, enc_field)

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
                    fields=tuple(group_fields),
                    toggle_field=group_def.toggle_field,
                )
            )
        return groups

    @staticmethod
    def _invalidate_cache() -> None:
        cache.delete(SITE_CONFIGURATION_CACHE_KEY)
        logger.info("Invalidated site configuration cache")


class WebFetchAuthHeader(models.Model):
    """
    Per-domain HTTP header used by the ``web_fetch`` tool when contacting a host.

    Values are stored encrypted via :class:`EncryptedFieldDescriptor`.
    """

    domain = models.CharField(_("domain"), max_length=255)
    header_name = models.CharField(_("header name"), max_length=255)

    _header_value_encrypted = models.TextField(blank=True, null=True, editable=False)

    header_value = EncryptedFieldDescriptor("header_value")

    class Meta:
        verbose_name = _("web fetch auth header")
        verbose_name_plural = _("web fetch auth headers")
        ordering = ("domain", "header_name")
        constraints = [models.UniqueConstraint(fields=("domain", "header_name"), name="unique_web_fetch_auth_header")]

    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

    def __str__(self) -> str:
        return f"{self.domain} → {self.header_name}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        super().save(*args, **kwargs)
        type(self).invalidate_cache()

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        result = super().delete(*args, **kwargs)
        type(self).invalidate_cache()
        return result

    def get_secret_hint(self) -> str | None:
        """Return a masked hint for the row's header value, or ``None`` if unset."""
        value = self.header_value
        if value is None:
            return None
        from core.encryption import mask_secret

        return mask_secret(value)

    @classmethod
    def _build_from_db(cls) -> dict[str, dict[str, Any]]:
        """Read all rows and build the grouped dict. Caller manages DB connections."""
        from pydantic import SecretStr

        out: dict[str, dict[str, SecretStr]] = {}
        for row in cls.objects.all():
            value = row.header_value
            if value is None:
                # ``EncryptedFieldDescriptor`` returns None when the column is
                # NULL or when decryption failed (e.g. after key rotation).
                # The descriptor already logs decryption errors; we log here
                # so operators can correlate "no auth headers sent" with
                # specific row PKs.
                logger.error("WebFetchAuthHeader row pk=%s has no readable value; skipping", row.pk)
                continue
            out.setdefault(row.domain, {})[row.header_name] = SecretStr(value)
        return out

    @classmethod
    def _load_and_cache(cls) -> dict[str, dict[str, Any]]:
        """
        Read all rows from the database, cache the grouped dict, and return it.

        Safe to call from any thread: manages DB connections via
        ``close_old_connections`` for ``ThreadPoolExecutor`` use. A broad
        ``except`` is intentional: ``web_fetch`` is on the agent's hot path
        and a DB outage / migration gap should degrade to "no auth headers"
        rather than crash the tool. The exception is logged so operators can
        diagnose the underlying failure.
        """
        from django.db import close_old_connections

        try:
            close_old_connections()
            out = cls._build_from_db()
        except Exception:  # noqa: BLE001 — degrade gracefully on DB/migration/encryption errors
            logger.exception("WebFetchAuthHeader rows not available; falling back to empty dict")
            return {}
        finally:
            with contextlib.suppress(Exception):
                close_old_connections()

        cache.set(WEB_FETCH_AUTH_HEADERS_CACHE_KEY, out, WEB_FETCH_AUTH_HEADERS_CACHE_TIMEOUT)
        return out

    @classmethod
    def get_cached(cls) -> dict[str, dict[str, Any]]:
        """
        Return all rows grouped by domain, with values wrapped in
        :class:`pydantic.SecretStr`.

        Async-safe: in async contexts the DB fallback runs in a
        :class:`ThreadPoolExecutor` to avoid Django's
        ``SynchronousOnlyOperation``. Cache hits are returned directly with
        no thread hop, since ``django.core.cache`` is async-safe.
        """
        cached = cache.get(WEB_FETCH_AUTH_HEADERS_CACHE_KEY)
        if cached is not None:
            return cached

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return cls._load_and_cache()

        try:
            return cls._executor.submit(cls._load_and_cache).result(timeout=5)
        except concurrent.futures.TimeoutError:
            logger.error("WebFetchAuthHeader lookup timed out (5s) in async context; falling back to empty dict")
            return {}
        except Exception:  # noqa: BLE001 — see _load_and_cache rationale
            logger.exception("WebFetchAuthHeader async lookup failed; falling back to empty dict")
            return {}

    @classmethod
    def invalidate_cache(cls) -> None:
        cache.delete(WEB_FETCH_AUTH_HEADERS_CACHE_KEY)


class Provider(models.Model):
    """
    Configurable model provider. Each row carries everything needed to call a
    provider — slug (used as ``slug:model_name`` prefix), wire protocol, base
    URL, encrypted API key, optional extra headers, and suggested model names.

    Four rows are seeded at migration time with ``is_locked=True`` so their
    slug and provider_type stay stable (``ModelName`` defaults reference them).
    """

    slug = models.SlugField(_("slug"), max_length=32, unique=True)
    display_name = models.CharField(_("display name"), max_length=64)
    provider_type = models.CharField(_("provider type"), max_length=32, choices=ProviderType.choices)
    base_url = models.URLField(_("base URL"), blank=True)
    _api_key_encrypted = models.TextField(blank=True, null=True, editable=False)
    api_key = EncryptedFieldDescriptor("api_key")
    extra_headers = models.JSONField(_("extra headers"), default=dict, blank=True)
    model_suggestions = models.TextField(_("model suggestions"), blank=True, help_text=_("One model name per line."))
    is_enabled = models.BooleanField(_("enabled"), default=True)
    is_locked = models.BooleanField(default=False, editable=False)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name = _("provider")
        verbose_name_plural = _("providers")
        ordering = ("sort_order", "slug")

    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # ``api_key`` is a descriptor (not a model field), so Django's default
        # ``__init__`` rejects it as an unknown keyword. Strip it out and set it
        # after the base initializer so the descriptor encrypts the plaintext.
        api_key = kwargs.pop("api_key", _UNSET)
        super().__init__(*args, **kwargs)
        if api_key is not _UNSET:
            self.api_key = api_key

    def __str__(self) -> str:
        return f"{self.display_name} ({self.slug})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.pk and self.is_locked:
            original = type(self).objects.only("slug", "provider_type").get(pk=self.pk)
            if original.slug != self.slug or original.provider_type != self.provider_type:
                raise ValueError(f"Provider {original.slug!r} is locked; slug and provider_type cannot change.")
        super().save(*args, **kwargs)
        type(self).invalidate_cache()

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        if self.is_locked:
            raise ValueError(f"Provider {self.slug!r} is locked and cannot be deleted.")
        result = super().delete(*args, **kwargs)
        type(self).invalidate_cache()
        return result

    def get_secret_hint(self) -> str | None:
        """Return a masked hint for the row's API key, or ``None`` if unset."""
        value = self.api_key
        if value is None:
            return None
        from core.encryption import mask_secret

        return mask_secret(value)

    @dataclass(frozen=True)
    class Cached:
        """Frozen, cache-safe snapshot of a Provider row."""

        slug: str
        display_name: str
        provider_type: str
        base_url: str
        api_key: Any  # pydantic.SecretStr | None
        extra_headers: dict
        model_suggestions_list: tuple[str, ...]
        is_enabled: bool
        is_locked: bool
        sort_order: int

    @classmethod
    def _build_from_db(cls) -> list[Cached]:
        from pydantic import SecretStr

        out: list[cls.Cached] = []
        for row in cls.objects.order_by("sort_order", "slug"):
            raw_key = row.api_key
            api_key = SecretStr(raw_key) if raw_key else None
            suggestions = tuple(line.strip() for line in row.model_suggestions.splitlines() if line.strip())
            out.append(
                cls.Cached(
                    slug=row.slug,
                    display_name=row.display_name,
                    provider_type=row.provider_type,
                    base_url=row.base_url,
                    api_key=api_key,
                    extra_headers=dict(row.extra_headers or {}),
                    model_suggestions_list=suggestions,
                    is_enabled=row.is_enabled,
                    is_locked=row.is_locked,
                    sort_order=row.sort_order,
                )
            )
        return out

    @classmethod
    def _load_and_cache(cls) -> list[Cached]:
        from django.db import close_old_connections

        try:
            close_old_connections()
            out = cls._build_from_db()
        except Exception:  # noqa: BLE001
            logger.exception("Provider rows not available; falling back to empty list")
            return []
        finally:
            with contextlib.suppress(Exception):
                close_old_connections()

        cache.set(PROVIDERS_CACHE_KEY, out, PROVIDERS_CACHE_TIMEOUT)
        return out

    @classmethod
    def get_cached_rows(cls) -> list[Cached]:
        cached = cache.get(PROVIDERS_CACHE_KEY)
        if cached is not None:
            return cached
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return cls._load_and_cache()
        try:
            return cls._executor.submit(cls._load_and_cache).result(timeout=5)
        except concurrent.futures.TimeoutError:
            logger.error("Provider lookup timed out (5s) in async context; returning empty list")
            return []
        except Exception:  # noqa: BLE001
            logger.exception("Provider async lookup failed; returning empty list")
            return []

    @classmethod
    def get_cached_by_slug(cls) -> dict[str, Provider.Cached]:
        return {row.slug: row for row in cls.get_cached_rows()}

    @classmethod
    def invalidate_cache(cls) -> None:
        cache.delete(PROVIDERS_CACHE_KEY)
