from __future__ import annotations

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from django_extensions.db.models import TimeStampedModel

from core.models import EncryptedJSONFieldDescriptor
from mcp_servers.constants import MCP_NAME_RE

_UNSET = object()


class MCPServer(TimeStampedModel):
    """An outbound MCP server connection. Source of truth for the MCP servers
    loaded at runtime (via ``mcp_servers.services.build_runtime_servers``); the
    file-based ``MCP_SERVERS_CONFIG_FILE`` is imported once by the 0002 migration
    and thereafter only checked on startup to emit a deprecation warning."""

    class Source(models.TextChoices):
        BUILTIN = "builtin", _("Built-in")
        CUSTOM = "custom", _("Custom")

    class Transport(models.TextChoices):
        HTTP = "http", _("HTTP (streamable)")
        SSE = "sse", _("SSE")

    class FilterMode(models.TextChoices):
        NONE = "none", _("None")
        ALLOW = "allow", _("Allow only listed tools")
        BLOCK = "block", _("Block listed tools")

    class HeaderMode(models.TextChoices):
        # Modes for entries in the JSON ``headers`` list (not a DB field): LITERAL stores the
        # value verbatim; ENV_REF stores an env-var name resolved at runtime. Single source of
        # truth for the mode strings shared by the form, services, and views.
        LITERAL = "literal", "literal"
        ENV_REF = "env_ref", "env_ref"

    name = models.SlugField(_("name"), max_length=80, unique=True, validators=[RegexValidator(regex=MCP_NAME_RE)])
    description = models.CharField(_("description"), max_length=1024, blank=True, default="")
    source = models.CharField(_("source"), max_length=10, choices=Source.choices, default=Source.CUSTOM)
    transport = models.CharField(_("transport"), max_length=10, choices=Transport.choices)
    url = models.URLField(_("URL"))
    _headers_encrypted = models.TextField(blank=True, null=True, editable=False)  # noqa: DJ001
    headers = EncryptedJSONFieldDescriptor("headers")
    tool_filter_mode = models.CharField(
        _("tool filter mode"), max_length=10, choices=FilterMode.choices, default=FilterMode.NONE
    )
    tool_filter_items = models.JSONField(_("tool filter items"), default=list, blank=True)
    enabled = models.BooleanField(_("enabled"), default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="mcp_servers"
    )

    class Meta:
        ordering = ["name"]
        constraints = [
            # Empty items with allow/block silently inverts the admin's intent
            # (allow-nothing / block-nothing); reject at the DB layer.
            models.CheckConstraint(
                condition=models.Q(tool_filter_mode="none") | ~models.Q(tool_filter_items=[]),
                name="mcp_tool_filter_items_required_when_mode_set",
            )
        ]

    def __init__(self, *args, **kwargs) -> None:
        # Mirror SandboxEnvironment: route ``headers`` through the descriptor so
        # ``Manager.create(headers=...)`` works on instantiation.
        headers_value = kwargs.pop("headers", _UNSET)
        super().__init__(*args, **kwargs)
        if headers_value is not _UNSET:
            self.headers = headers_value

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs) -> None:
        # Backstops clean_name's rename guard so it holds for any caller, not just
        # MCPServerForm — name is used as a stable key (URLs, cache keys, tool-filter prefix).
        if self.pk is not None:
            original_name = type(self).objects.filter(pk=self.pk).values_list("name", flat=True).first()
            if original_name is not None and original_name != self.name:
                raise ValueError(
                    f"Cannot rename MCP server {original_name!r} to {self.name!r}; delete and re-create instead."
                )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        # Backstops the view's source=CUSTOM queryset filter so this holds for any caller.
        if self.is_builtin():
            raise ValueError(f"Cannot delete built-in MCP server {self.name!r}.")
        return super().delete(*args, **kwargs)

    def is_builtin(self) -> bool:
        """Whether this row is a code-defined built-in (vs. an admin-created
        custom server). Built-in rows are seeded from ``mcp_servers.seeds``,
        cannot be renamed or deleted, but are otherwise fully editable — this
        row is the source of truth for connection details (URL, headers,
        tool filter, enabled)."""
        return self.source == self.Source.BUILTIN
