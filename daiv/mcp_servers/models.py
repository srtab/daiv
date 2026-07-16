from __future__ import annotations

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.validators import RegexValidator
from django.db import models
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _

from django_extensions.db.models import TimeStampedModel

from core.models import EncryptedJSONFieldDescriptor
from mcp_servers.constants import MCP_NAME_RE
from mcp_servers.validators import validate_http_url

_UNSET = object()


class MCPServerQuerySet(models.QuerySet):
    """Scoping helpers shared by views and runtime. GLOBAL rows are admin-managed
    and visible to everyone; USER rows are owned by one user and load only in that
    user's runs.

    Authorization failures deliberately signal differently by scope: a forbidden
    GLOBAL row raises ``PermissionDenied`` (its existence is not secret), while a
    non-owned USER row raises ``Http404`` (so one member cannot probe for the
    existence of another's personal servers)."""

    def global_servers(self):
        # Scope alone is authoritative — a global server may have any ``Source``.
        return self.filter(scope=MCPServer.Scope.GLOBAL)

    def user_servers(self, user):
        return self.filter(scope=MCPServer.Scope.USER, user=user).order_by("name")

    def scoped_get(self, user, pk):
        """Fetch ``pk`` for an EDIT. GLOBAL → admin only (``PermissionDenied``);
        USER → owner only (``Http404`` otherwise). Admins may NOT edit another
        user's personal server — only manage it (see ``manageable_get``)."""
        server = get_object_or_404(self, pk=pk)
        if server.scope == MCPServer.Scope.GLOBAL:
            if not user.is_admin:
                raise PermissionDenied("Admin required for global MCP servers")
            return server
        if server.user_id != user.id:
            raise Http404("Not found")
        return server

    def manageable_get(self, user, pk):
        """Fetch ``pk`` for a MANAGE action (enable/disable, delete, refresh).
        GLOBAL → admin only; USER → owner or admin."""
        server = get_object_or_404(self, pk=pk)
        if server.scope == MCPServer.Scope.GLOBAL:
            if not user.is_admin:
                raise PermissionDenied("Admin required for global MCP servers")
            return server
        if server.user_id != user.id and not user.is_admin:
            raise Http404("Not found")
        return server


class MCPServer(TimeStampedModel):
    """An outbound MCP server connection. Source of truth for the MCP servers
    loaded at runtime (via ``mcp_servers.services.build_runtime_servers``). A
    legacy ``MCP_SERVERS_CONFIG_FILE`` JSON config, if set, is imported once by
    the 0002 migration and ignored thereafter."""

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

    class Scope(models.TextChoices):
        GLOBAL = "global", _("Global")
        USER = "user", _("User")

    name = models.SlugField(_("name"), max_length=80, validators=[RegexValidator(regex=MCP_NAME_RE)])
    description = models.CharField(_("description"), max_length=1024, blank=True, default="")
    source = models.CharField(_("source"), max_length=10, choices=Source.choices, default=Source.CUSTOM)
    scope = models.CharField(_("scope"), max_length=10, choices=Scope.choices, default=Scope.GLOBAL)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True, related_name="owned_mcp_servers"
    )
    transport = models.CharField(_("transport"), max_length=10, choices=Transport.choices)
    # CharField, not URLField (varchar(200) either way): URLValidator rejects internal hosts —
    # single-label names and underscores, e.g. a Docker service like ``mcp_rt`` — that MCP servers
    # use on the internal network. Validated by validate_http_url on the form / full_clean path;
    # see its docstring for the full rationale.
    url = models.CharField(_("URL"), max_length=200, validators=[validate_http_url])
    _headers_encrypted = models.TextField(blank=True, null=True, editable=False)  # noqa: DJ001
    headers = EncryptedJSONFieldDescriptor("headers")
    tool_filter_mode = models.CharField(
        _("tool filter mode"), max_length=10, choices=FilterMode.choices, default=FilterMode.NONE
    )
    tool_filter_items = models.JSONField(_("tool filter items"), default=list, blank=True)
    enabled = models.BooleanField(_("enabled"), default=True)
    discovered_tools = models.JSONField(_("discovered tools"), default=list, blank=True, editable=False)
    tools_synced_at = models.DateTimeField(_("tools synced at"), null=True, blank=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="mcp_servers"
    )

    objects = MCPServerQuerySet.as_manager()

    class Meta:
        ordering = ["name"]
        constraints = [
            # Empty items with allow/block silently inverts the admin's intent
            # (allow-nothing / block-nothing); reject at the DB layer.
            models.CheckConstraint(
                condition=models.Q(tool_filter_mode="none") | ~models.Q(tool_filter_items=[]),
                name="mcp_tool_filter_items_required_when_mode_set",
            ),
            models.UniqueConstraint(fields=["name"], condition=models.Q(scope="global"), name="mcp_global_name_unique"),
            models.UniqueConstraint(
                fields=["user", "name"], condition=models.Q(scope="user"), name="mcp_user_name_unique"
            ),
            models.CheckConstraint(
                condition=(
                    (models.Q(scope="user") & models.Q(user__isnull=False))
                    | (models.Q(scope="global") & models.Q(user__isnull=True))
                ),
                name="mcp_scope_user_shape",
            ),
            models.CheckConstraint(
                condition=~(models.Q(source="builtin") & models.Q(scope="user")), name="mcp_builtin_is_global"
            ),
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
        # MCPServerForm — name is used as a stable key (URLs, built-in seed lookup, tool-filter prefix).
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

    @property
    def is_user_scoped(self) -> bool:
        return self.scope == self.Scope.USER

    def is_shadowed_by(self, global_names) -> bool:
        """Whether this personal server is superseded at runtime by a global server
        of the same name. ``build_runtime_servers`` skips such rows (global wins) and
        the list page flags them 'Shadowed'. ``global_names`` is the set of existing
        global-server names."""
        return self.is_user_scoped and self.name in global_names
