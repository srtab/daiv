from __future__ import annotations

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.validators import RegexValidator
from django.db import models
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _

from django_extensions.db.models import TimeStampedModel

from plugins.constants import (
    MAX_DESCRIPTION_CHARS,
    MAX_PLUGIN_PATH_CHARS,
    MAX_REPO_URL_CHARS,
    MAX_VERSION_CHARS,
    PLUGIN_NAME_RE,
)


class PluginQuerySet(models.QuerySet):
    """Global plugins are admin-managed and visible to everyone; user plugins belong to one user.

    A forbidden global plugin raises ``PermissionDenied`` (its existence is not secret); a
    non-owned user plugin raises ``Http404`` so members cannot probe for others' plugins."""

    def global_plugins(self):
        return self.filter(scope=Plugin.Scope.GLOBAL)

    def user_plugins(self, user):
        return self.filter(scope=Plugin.Scope.USER, user=user).order_by("name")

    def scoped_get(self, user, pk):
        plugin = get_object_or_404(self, pk=pk)
        if plugin.scope == Plugin.Scope.USER and plugin.user_id != user.id and not user.is_admin:
            raise Http404("Not found")
        return plugin

    def manageable_get(self, user, pk):
        plugin = get_object_or_404(self, pk=pk)
        if plugin.scope == Plugin.Scope.GLOBAL:
            if not user.is_admin:
                raise PermissionDenied("Admin required for global plugins")
            return plugin
        if plugin.user_id != user.id and not user.is_admin:
            raise Http404("Not found")
        return plugin

    def loadable(self):
        return self.filter(is_active=True, access_lost_at__isnull=True).exclude(installed_commit="")


class Plugin(TimeStampedModel):
    class Scope(models.TextChoices):
        GLOBAL = "global", _("Global")
        USER = "user", _("User")

    scope = models.CharField(_("scope"), max_length=10, choices=Scope.choices, default=Scope.GLOBAL)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True, related_name="plugins"
    )
    installed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    name = models.SlugField(_("name"), max_length=80, validators=[RegexValidator(regex=PLUGIN_NAME_RE)])
    repo_url = models.CharField(_("repository URL"), max_length=MAX_REPO_URL_CHARS)
    plugin_path = models.CharField(_("plugin path"), max_length=MAX_PLUGIN_PATH_CHARS, blank=True, default="")
    description = models.CharField(_("description"), max_length=MAX_DESCRIPTION_CHARS, blank=True, default="")
    version = models.CharField(_("version"), max_length=MAX_VERSION_CHARS, blank=True, default="")
    installed_commit = models.CharField(_("installed commit"), max_length=40)
    contents = models.JSONField(_("contents"), default=dict, blank=True)
    is_active = models.BooleanField(_("active"), default=True)
    auto_update = models.BooleanField(_("auto update"), default=True)
    last_checked_at = models.DateTimeField(_("last checked at"), null=True, blank=True)
    last_error = models.TextField(_("last error"), blank=True, default="")
    failed_commit = models.CharField(_("failed commit"), max_length=40, blank=True, default="")
    access_lost_at = models.DateTimeField(_("access lost at"), null=True, blank=True)

    objects = PluginQuerySet.as_manager()

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["name"], condition=models.Q(scope="global"), name="plugin_global_name_unique"
            ),
            models.UniqueConstraint(
                fields=["user", "name"], condition=models.Q(scope="user"), name="plugin_user_name_unique"
            ),
            models.CheckConstraint(
                condition=(
                    (models.Q(scope="user") & models.Q(user__isnull=False))
                    | (models.Q(scope="global") & models.Q(user__isnull=True))
                ),
                name="plugin_scope_user_shape",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class PluginOptOut(TimeStampedModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="plugin_opt_outs")
    plugin = models.ForeignKey(Plugin, on_delete=models.CASCADE, related_name="opt_outs")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "plugin"], name="plugin_opt_out_unique")]

    def __str__(self) -> str:
        return f"{self.user_id} opted out of {self.plugin_id}"
