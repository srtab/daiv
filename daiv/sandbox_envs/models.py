from __future__ import annotations

import re
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import NON_FIELD_ERRORS, PermissionDenied, ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _

from django_extensions.db.models import TimeStampedModel

from core.models import EncryptedJSONFieldDescriptor

_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REPO_ID_RE = re.compile(r"^[^\s/]+(/[^\s/]+)+$")
ENV_VARS_MAX_ENTRIES = 100
ENV_VARS_MAX_ENCRYPTED_SIZE = 32 * 1024  # 32 KiB
EGRESS_MAX_RULES = 100
EGRESS_MAX_SECRETS = 100
EGRESS_SECRETS_MAX_ENCRYPTED_SIZE = 32 * 1024  # 32 KiB

_UNSET = object()


class Scope(models.TextChoices):
    USER = "user", _("User")
    GLOBAL = "global", _("Global")


def _fmt_memory(mem: int) -> str:
    return f"{mem // 2**30} GiB" if mem % 2**30 == 0 else f"{mem // 2**20} MiB"


def _fmt_cpus(cpus) -> str:
    d = cpus if isinstance(cpus, Decimal) else Decimal(cpus)
    return str(int(d)) if d == d.to_integral_value() else str(d.normalize())


class SandboxEnvironmentQuerySet(models.QuerySet):
    """Queryset helpers consumed by views, services, and external callers
    (chat, activity, mcp_server). Methods live here rather than in services
    so they're chainable and easy to compose with ``filter`` / ``annotate``."""

    def global_envs(self):
        return self.filter(scope=Scope.GLOBAL).order_by("name")

    def user_envs(self, user):
        return self.filter(scope=Scope.USER, user=user).order_by("name")

    def global_default(self):
        return self.filter(scope=Scope.GLOBAL, is_default=True).first()

    async def aglobal_default(self):
        return await self.filter(scope=Scope.GLOBAL, is_default=True).afirst()

    def visible_to(self, user):
        return self.filter(Q(scope=Scope.USER, user=user) | Q(scope=Scope.GLOBAL)).order_by("scope", "name")

    def scoped_get(self, user, pk):
        """Return the env at ``pk`` if ``user`` may operate on it.

        - non-owner USER env → ``Http404``
        - GLOBAL env with non-admin user → ``PermissionDenied``
        - missing pk → ``Http404``
        """
        env = get_object_or_404(self, pk=pk)
        if env.scope == Scope.GLOBAL:
            if not user.is_admin:
                raise PermissionDenied("Admin required for global environments")
            return env
        if env.user_id != user.id:
            raise Http404("Not found")
        return env


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

    # Per-env egress policy provisioned to the daiv-sandbox sidecar proxy when network is enabled.
    # ``None`` = no egress policy configured for this env; a policy is stored only when the env defines
    # at least one allowed-host rule. The daiv-sandbox egress proxy is mandatory for network-enabled
    # sessions: a network-enabled env on a sandbox with no egress proxy configured (no shared egress CA)
    # is rejected at session start — there is no raw-network fallback.
    # NOTE: when the proxy IS configured, a network-enabled env with egress_policy=None gets the
    # sidecar's default deny-all (no connectivity) — configure a policy to grant connectivity.
    egress_policy = models.JSONField(_("egress policy"), null=True, blank=True, default=None)
    _egress_secrets_encrypted = models.TextField(blank=True, null=True, editable=False)  # noqa: DJ001 — NULL = no secrets
    egress_secrets = EncryptedJSONFieldDescriptor("egress_secrets")

    objects = SandboxEnvironmentQuerySet.as_manager()

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
        # Route ``env_vars``/``egress_secrets`` through their descriptors so ``Manager.create(...)`` works.
        env_vars_value = kwargs.pop("env_vars", _UNSET)
        egress_secrets_value = kwargs.pop("egress_secrets", _UNSET)
        super().__init__(*args, **kwargs)
        if env_vars_value is not _UNSET:
            self.env_vars = env_vars_value
        if egress_secrets_value is not _UNSET:
            self.egress_secrets = egress_secrets_value

    def __str__(self) -> str:
        return f"{self.get_scope_display()}: {self.name}"

    @property
    def summary(self) -> str:
        """Compact one-line description of this env's runtime shape.

        Format: ``"<base_image> · <N> CPU · <memory> · net"`` with each
        segment omitted when its source value is missing. Memory renders
        as ``GiB`` for whole-GiB values, ``MiB`` otherwise. Network is
        included only when explicitly enabled (True)."""
        parts: list[str] = []
        if self.base_image:
            parts.append(self.base_image)
        if self.cpus is not None:
            parts.append(f"{_fmt_cpus(self.cpus)} CPU")
        if self.memory_bytes is not None:
            parts.append(_fmt_memory(self.memory_bytes))
        if self.network_enabled is True:
            parts.append("net")
        return " · ".join(parts)

    @property
    def short_summary(self) -> str:
        """Compact ``"<base_image>"`` description, with ``" · net"`` appended
        when network access is explicitly enabled. Segments are omitted when
        their source value is missing."""
        parts: list[str] = []
        if self.base_image:
            parts.append(self.base_image)
        if self.network_enabled is True:
            parts.append("net")
        return " · ".join(parts)

    @property
    def is_global_default(self) -> bool:
        """True iff this row is the single GLOBAL env currently marked default.

        The explicit ``scope == GLOBAL`` guard is redundant for persisted rows
        (the ``env_default_only_on_global`` CheckConstraint enforces it) but
        defends unsaved/in-memory instances against the disallowed combo.
        """
        return self.scope == Scope.GLOBAL and bool(self.is_default)

    def can_delete(self) -> tuple[bool, str | None]:
        """Row-level invariant gate for delete. ``promote_as_default`` must run
        first if this is the global default."""
        if self.is_global_default:
            return False, str(_("Set another global environment as default before deleting this one."))
        return True, None

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
        self._validate_repo_ids()
        self._validate_egress()

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

    def _validate_repo_ids(self) -> None:
        raw = self.repo_ids or []
        if not isinstance(raw, list):
            raise ValidationError({"repo_ids": _("repo_ids must be a list.")})
        cleaned: list[str] = []
        seen: set[str] = set()
        for idx, entry in enumerate(raw):
            if not isinstance(entry, str):
                raise ValidationError({"repo_ids": _("repo_ids[%d] must be a string.") % idx})
            value = entry.strip()
            if not value:
                raise ValidationError({"repo_ids": _("repo_ids[%d] cannot be blank.") % idx})
            if not _REPO_ID_RE.match(value):
                raise ValidationError({
                    "repo_ids": _(
                        "Invalid repo id '%(value)s' at index %(idx)d. Use a slash-separated path "
                        "like 'owner/repo' or 'group/subgroup/repo'."
                    )
                    % {"value": value, "idx": idx}
                })
            if value in seen:
                raise ValidationError({"repo_ids": _("Duplicate repo id '%s' in this env.") % value})
            seen.add(value)
            cleaned.append(value)
        self.repo_ids = cleaned
        if not cleaned:
            return
        qs = SandboxEnvironment.objects.filter(scope=self.scope)
        if self.scope == Scope.USER:
            qs = qs.filter(user=self.user)
        if self.pk is not None:
            qs = qs.exclude(pk=self.pk)
        for other in qs.only("id", "name", "repo_ids"):
            overlap = sorted(set(other.repo_ids or []) & set(cleaned))
            if overlap:
                raise ValidationError({
                    "repo_ids": _("Repo id(s) %(repos)s already claimed by environment '%(name)s'.")
                    % {"repos": ", ".join(overlap), "name": other.name}
                })

    def _validate_egress(self) -> None:
        from pydantic import ValidationError as PydanticValidationError

        from core.encryption import DecryptionError
        from core.sandbox.schemas import EgressConfigRequest

        if self.egress_policy is None:
            return  # no egress configured
        if not isinstance(self.egress_policy, dict):
            raise ValidationError({"egress_policy": _("Egress policy must be an object.")})

        raw = self._egress_secrets_encrypted or ""
        if len(raw) > EGRESS_SECRETS_MAX_ENCRYPTED_SIZE:
            raise ValidationError({"egress_secrets": _("Egress secrets exceed 32 KiB encrypted.")})
        try:
            secrets_raw = self.egress_secrets or {}
        except DecryptionError as err:
            raise ValidationError({
                NON_FIELD_ERRORS: _(
                    "Existing egress secrets could not be decrypted. Re-enter all secret values, "
                    "or restore DAIV_ENCRYPTION_KEY."
                )
            }) from err
        if not isinstance(secrets_raw, dict):
            raise ValidationError({"egress_secrets": _("Egress secrets must be an object.")})
        if len(self.egress_policy.get("rules") or []) > EGRESS_MAX_RULES:
            raise ValidationError({"egress_policy": _("Too many egress rules (max %d).") % EGRESS_MAX_RULES})
        if len(secrets_raw) > EGRESS_MAX_SECRETS:
            raise ValidationError({"egress_secrets": _("Too many egress secrets (max %d).") % EGRESS_MAX_SECRETS})

        # Authoritative shape + inject-resolves check (count/size caps handled above): build the wire request.
        try:
            EgressConfigRequest.from_stored(self.egress_policy, secrets_raw)
        except (PydanticValidationError, TypeError, ValueError) as err:
            raise ValidationError({"egress_policy": _("Invalid egress configuration: %s") % err}) from err

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
