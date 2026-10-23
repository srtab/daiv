from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from django_extensions.db.models import TimeStampedModel

from accounts.managers import APIKeyManager
from codebase.base import GitPlatform
from core.models import EncryptedFieldDescriptor


class Role(models.TextChoices):
    ADMIN = "admin", _("Admin")
    MEMBER = "member", _("Member")


class User(AbstractUser):
    email = models.EmailField(_("email address"), unique=True)
    name = models.CharField(_("name"), max_length=128, blank=True)
    role = models.CharField(_("role"), max_length=10, choices=Role.choices, default=Role.MEMBER)

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


_CREDENTIAL_PROVIDERS = (GitPlatform.GITLAB, GitPlatform.GITHUB)
_CREDENTIAL_PROVIDER_LABELS = {GitPlatform.GITLAB: _("GitLab"), GitPlatform.GITHUB: _("GitHub")}
CREDENTIAL_PROVIDER_CHOICES = [(p.value, _CREDENTIAL_PROVIDER_LABELS[p]) for p in _CREDENTIAL_PROVIDERS]


class CredentialState(models.TextChoices):
    CONNECTED = "connected", _("Connected")
    EXPIRED = "expired", _("Expired")
    REVOKED = "revoked", _("Revoked")


class PlatformCredential(TimeStampedModel):
    """A person's own OAuth grant on a git platform, used only for calls beyond the attached project.

    ``accounts.credentials`` is the only module that reads the plaintext. Expired and revoked rows
    hold no secrets.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="platform_credentials")
    provider = models.CharField(_("provider"), max_length=10, choices=CREDENTIAL_PROVIDER_CHOICES)
    host = models.CharField(_("host"), max_length=255)
    platform_uid = models.CharField(_("platform user ID"), max_length=191)
    _access_token_encrypted = models.TextField(_("access token"), null=True, blank=True)  # noqa: DJ001
    _refresh_token_encrypted = models.TextField(_("refresh token"), null=True, blank=True)  # noqa: DJ001
    expires_at = models.DateTimeField(_("expires at"), null=True, blank=True)
    scopes = models.JSONField(_("granted scopes"), default=list, blank=True)
    state = models.CharField(
        _("state"), max_length=10, choices=CredentialState.choices, default=CredentialState.CONNECTED
    )

    access_token = EncryptedFieldDescriptor("access_token")
    refresh_token = EncryptedFieldDescriptor("refresh_token")

    class Meta:
        verbose_name = _("Platform Credential")
        verbose_name_plural = _("Platform Credentials")
        constraints = [
            models.UniqueConstraint(fields=["user", "provider", "host"], name="platform_credential_unique"),
            models.CheckConstraint(
                condition=models.Q(expires_at__isnull=True) | models.Q(_refresh_token_encrypted__isnull=False),
                name="platform_credential_expiring_needs_refresh",
            ),
            models.CheckConstraint(
                condition=~models.Q(state=CredentialState.CONNECTED) | models.Q(_access_token_encrypted__isnull=False),
                name="platform_credential_connected_needs_token",
            ),
        ]
        indexes = [models.Index(fields=["provider", "platform_uid"])]

    def __str__(self) -> str:
        return f"{self.user} @ {self.provider} ({self.state})"

    def clean(self) -> None:
        super().clean()
        if self.expires_at is not None and self._refresh_token_encrypted is None:
            raise ValidationError({
                "expires_at": _("An expiring credential must carry a refresh token, or it can never be renewed.")
            })
        if self.state == CredentialState.CONNECTED and self._access_token_encrypted is None:
            raise ValidationError({"state": _("A connected credential must carry an access token.")})

    def save(self, *args, **kwargs) -> None:
        self.clean()
        super().save(*args, **kwargs)
