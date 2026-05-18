from __future__ import annotations

import re
import uuid

from django.conf import settings
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.db import models, transaction
from django.utils.translation import gettext_lazy as _

from django_extensions.db.models import TimeStampedModel

from core.models import EncryptedJSONFieldDescriptor

_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENV_VARS_MAX_ENTRIES = 100
ENV_VARS_MAX_ENCRYPTED_SIZE = 32 * 1024  # 32 KiB

_UNSET = object()


class Scope(models.TextChoices):
    USER = "user", _("User")
    GLOBAL = "global", _("Global")


class SandboxEnvironment(TimeStampedModel):
    """A named sandbox configuration. USER envs are owned by one user; GLOBAL envs
    are admin-managed and visible to everyone. Exactly one GLOBAL env may be marked
    ``is_default=True`` — that's the one the runtime resolver uses when no per-run
    env is selected."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scope = models.CharField(max_length=10, choices=Scope.choices, default=Scope.USER)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sandbox_environments", null=True, blank=True
    )
    name = models.CharField(_("name"), max_length=80)
    description = models.TextField(_("description"), blank=True, default="")

    base_image = models.CharField(_("base image"), max_length=255, blank=True, default="")
    network_enabled = models.BooleanField(_("network enabled"), null=True, blank=True, default=None)
    memory_bytes = models.PositiveBigIntegerField(_("memory (bytes)"), null=True, blank=True)
    cpus = models.DecimalField(_("CPUs"), max_digits=5, decimal_places=2, null=True, blank=True)

    _env_vars_encrypted = models.TextField(blank=True, null=True, editable=False)  # noqa: DJ001 — NULL means "no env vars set"
    env_vars = EncryptedJSONFieldDescriptor("env_vars")

    repo_ids = models.JSONField(_("repo ids"), default=list, blank=True)

    is_default = models.BooleanField(_("is default"), default=False)

    class Meta:
        ordering = ["scope", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "name"], condition=models.Q(scope="user"), name="env_user_name_unique"
            ),
            models.UniqueConstraint(fields=["name"], condition=models.Q(scope="global"), name="env_global_name_unique"),
            models.UniqueConstraint(
                fields=["is_default"],
                condition=models.Q(scope="global") & models.Q(is_default=True),
                name="env_one_global_default",
            ),
            models.CheckConstraint(
                condition=(
                    (models.Q(scope="user") & models.Q(user__isnull=False))
                    | (models.Q(scope="global") & models.Q(user__isnull=True))
                ),
                name="env_scope_user_shape",
            ),
            models.CheckConstraint(
                condition=models.Q(is_default=False) | models.Q(scope="global"), name="env_default_only_on_global"
            ),
        ]

    def __init__(self, *args, **kwargs) -> None:
        # Route ``env_vars`` through the descriptor so ``Manager.create(env_vars=...)`` works.
        env_vars_value = kwargs.pop("env_vars", _UNSET)
        super().__init__(*args, **kwargs)
        if env_vars_value is not _UNSET:
            self.env_vars = env_vars_value

    def __str__(self) -> str:
        return f"{self.get_scope_display()}: {self.name}"

    def clean(self) -> None:
        super().clean()
        name = (self.name or "").strip()
        if not name:
            raise ValidationError({"name": _("Name is required.")})
        self.name = name
        if not (self.base_image or "").strip():
            raise ValidationError({"base_image": _("Base image is required.")})
        self.base_image = self.base_image.strip()
        self._validate_env_vars()

    def _validate_env_vars(self) -> None:
        from core.encryption import DecryptionError

        raw = self._env_vars_encrypted or ""
        if len(raw) > ENV_VARS_MAX_ENCRYPTED_SIZE:
            raise ValidationError({"env_vars": _("Environment variables exceed 32 KiB encrypted.")})
        try:
            values = self.env_vars or []
        except DecryptionError as err:
            # Existing ciphertext is unreadable. We refuse to validate-then-save so
            # the row's still-valid ciphertext isn't overwritten with placeholder
            # content. The user must re-enter all secret values explicitly.
            # Use NON_FIELD_ERRORS rather than ``env_vars`` since the descriptor
            # is not a form field — the form's ``_post_clean`` would otherwise
            # raise ``ValueError`` trying to attach the error to a missing field.
            raise ValidationError({
                NON_FIELD_ERRORS: _(
                    "Existing environment variables could not be decrypted. Re-enter all secret values, "
                    "or restore DAIV_ENCRYPTION_KEY."
                )
            }) from err
        if len(values) > ENV_VARS_MAX_ENTRIES:
            raise ValidationError({"env_vars": _("Too many environment variables (max %d).") % ENV_VARS_MAX_ENTRIES})
        seen: set[str] = set()
        for idx, entry in enumerate(values):
            name = (entry.get("name") or "").strip()
            if not _ENV_VAR_NAME_RE.match(name):
                raise ValidationError({
                    "env_vars": _("Invalid env var name '%(name)s' at index %(idx)d.") % {"name": name, "idx": idx}
                })
            if name in seen:
                raise ValidationError({"env_vars": _("Duplicate env var name '%s'.") % name})
            seen.add(name)

    def promote_as_default(self) -> None:
        """Atomically demote any other GLOBAL default and mark this env as default.

        Used by admin flows to swap which GLOBAL env is the default without
        running into the partial unique index ``env_one_global_default``.

        Re-reads the row under ``SELECT ... FOR UPDATE`` to guard against a
        stale in-memory ``scope`` (e.g. a concurrent admin demoting the env
        before this call) and runs ``full_clean()`` so model-level invariants
        are enforced rather than left to the DB CheckConstraint.
        """
        with transaction.atomic():
            try:
                fresh = SandboxEnvironment.objects.select_for_update().get(pk=self.pk)
            except SandboxEnvironment.DoesNotExist as err:
                raise ValidationError(_("Sandbox environment no longer exists.")) from err
            if fresh.scope != Scope.GLOBAL:
                raise ValidationError(_("Only GLOBAL environments can be marked as default."))
            SandboxEnvironment.objects.filter(scope=Scope.GLOBAL, is_default=True).exclude(pk=fresh.pk).update(
                is_default=False
            )
            fresh.is_default = True
            fresh.full_clean()
            fresh.save()
        self.is_default = True
        self.scope = fresh.scope
