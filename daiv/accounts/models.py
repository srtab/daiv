from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _

from django_extensions.db.models import TimeStampedModel
from notifications.choices import NotifyOn

from accounts.managers import APIKeyManager


class Role(models.TextChoices):
    ADMIN = "admin", _("Admin")
    MEMBER = "member", _("Member")


class User(AbstractUser):
    email = models.EmailField(_("email address"), unique=True)
    name = models.CharField(_("name"), max_length=128, blank=True)
    role = models.CharField(_("role"), max_length=10, choices=Role.choices, default=Role.MEMBER)
    notify_on_jobs = models.CharField(
        _("notify on jobs"),
        max_length=16,
        choices=NotifyOn.choices,
        default=NotifyOn.ON_FAILURE,
        help_text=_("When to receive notifications for agent runs you start (UI/API/MCP)."),
    )

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    def is_last_active_admin(self) -> bool:
        """Check if this user is the only active admin in the system."""
        if self.role != Role.ADMIN or not self.is_active:
            return False
        return not User.objects.filter(role=Role.ADMIN, is_active=True).exclude(pk=self.pk).exists()

    def __str__(self):
        return self.get_full_name() or self.name or self.username or self.email


class APIKey(TimeStampedModel):
    """
    API Key model to allow users to authenticate with the API.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_keys")
    name = models.CharField(_("name"), max_length=128, blank=True)
    prefix = models.CharField(_("prefix"), max_length=8, unique=True)
    hashed_key = models.CharField(_("API Key"), max_length=256, unique=True)
    expires_at = models.DateTimeField(_("expires at"), null=True, blank=True)
    revoked = models.BooleanField(_("revoked"), default=False)

    objects: APIKeyManager[APIKey] = APIKeyManager()

    class Meta:
        verbose_name = _("API Key")
        verbose_name_plural = _("API Keys")

    def __str__(self):
        return f"{self.name} ({self.user})"
