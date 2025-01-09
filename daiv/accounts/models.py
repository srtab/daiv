from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _

from django_extensions.db.models import TimeStampedModel

from accounts.managers import APIKeyManager


class User(AbstractUser):
    email = models.EmailField(_("email address"), unique=True)
    name = models.CharField(_("name"), max_length=128, blank=True)

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
