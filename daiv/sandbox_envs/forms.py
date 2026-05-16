from __future__ import annotations

import json

from django import forms
from django.utils.translation import gettext_lazy as _

from sandbox_envs.models import _ENV_VAR_NAME_RE, ENV_VARS_MAX_ENTRIES, SandboxEnvironment, Scope
from sandbox_envs.services import FIELD_TO_LOCK_SETTING


class SandboxEnvironmentForm(forms.ModelForm):
    """Form for creating/editing a SandboxEnvironment.

    * Non-admins cannot select scope=GLOBAL.
    * When editing the GLOBAL default, env-locked fields are read-only and pre-filled
      from ``site_settings``; submitted values for those fields are ignored.
    """

    env_vars_json = forms.CharField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = SandboxEnvironment
        fields = ("name", "description", "scope", "base_image", "network_enabled", "memory_bytes", "cpus", "is_default")

    def __init__(self, *args, user=None, is_admin=False, is_default_form=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.is_admin = is_admin
        self.is_default_form = is_default_form
        if not is_admin:
            # Non-admins cannot select GLOBAL. We restrict the available choices
            # rather than disabling the field, so a submitted GLOBAL value is
            # rejected by clean_scope / choice validation instead of being
            # silently coerced to the initial value.
            self.fields["scope"].choices = [(Scope.USER.value, Scope.USER.label)]
            self.fields["scope"].initial = Scope.USER
            self.fields.pop("is_default", None)
        if is_default_form:
            self._apply_env_locks()

    def _apply_env_locks(self) -> None:
        from core.site_settings import site_settings

        for form_field, settings_name in FIELD_TO_LOCK_SETTING.items():
            if not site_settings.is_env_locked(settings_name):
                continue
            field = self.fields.get(form_field)
            if field is None:
                continue
            field.disabled = True
            value = getattr(site_settings, settings_name, None)
            if value is not None:
                self.initial[form_field] = value
            field.help_text = _("Set by environment variable; edit DAIV_%s to change.") % settings_name.upper()

    def clean_scope(self):
        scope = self.cleaned_data["scope"]
        if scope == Scope.GLOBAL and not self.is_admin:
            raise forms.ValidationError(_("Only administrators can create global environments."))
        return scope

    def clean_env_vars_json(self):
        raw = (self.cleaned_data.get("env_vars_json") or "").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as err:
            raise forms.ValidationError(_("env_vars must be valid JSON.")) from err
        if not isinstance(parsed, list):
            raise forms.ValidationError(_("env_vars must be a list."))
        if len(parsed) > ENV_VARS_MAX_ENTRIES:
            raise forms.ValidationError(_("Too many environment variables (max %d).") % ENV_VARS_MAX_ENTRIES)
        seen: set[str] = set()
        cleaned: list[dict] = []
        for idx, entry in enumerate(parsed):
            if not isinstance(entry, dict):
                raise forms.ValidationError(_("env_vars entry at index %d must be an object.") % idx)
            entry_dict: dict = entry  # narrow for ty
            name = (entry_dict.get("name") or "").strip()
            if not _ENV_VAR_NAME_RE.match(name):
                raise forms.ValidationError(
                    _("Invalid env var name '%(name)s' at index %(idx)d.") % {"name": name, "idx": idx}
                )
            if name in seen:
                raise forms.ValidationError(_("Duplicate env var name '%s'.") % name)
            seen.add(name)
            cleaned.append({
                "name": name,
                "value": entry_dict.get("value", ""),
                "is_secret": bool(entry_dict.get("is_secret", False)),
            })
        return cleaned

    def save(self, commit: bool = True) -> SandboxEnvironment:
        env_vars = self.cleaned_data.get("env_vars_json") or []
        instance: SandboxEnvironment = super().save(commit=False)
        if instance.scope == Scope.USER and self.user is not None:
            instance.user = self.user
        if instance.pk is not None:
            env_vars = self._preserve_unchanged_secrets(instance, env_vars)
        instance.env_vars = env_vars
        if commit:
            instance.full_clean()
            instance.save()
        return instance

    @staticmethod
    def _preserve_unchanged_secrets(instance: SandboxEnvironment, submitted_rows: list[dict]) -> list[dict]:
        """Restore the stored value for any submitted secret row whose value is
        empty (the masked sentinel) or the literal ``"******"`` mask. Matched by
        env-var name against the instance's currently persisted env_vars.

        Raises a form-level :class:`~django.core.exceptions.ValidationError` if
        existing ciphertext cannot be decrypted — proceeding would otherwise
        persist the ``"******"`` mask as the new value, destroying the secret.
        """
        from core.encryption import DecryptionError

        try:
            existing_rows = instance.env_vars or []
        except DecryptionError as err:
            raise forms.ValidationError(
                _(
                    "Existing environment variables could not be decrypted. Re-enter all secret values "
                    "before saving, or restore DAIV_ENCRYPTION_KEY."
                )
            ) from err
        existing = {r["name"]: r["value"] for r in existing_rows if r.get("name")}
        out: list[dict] = []
        for row in submitted_rows:
            name = row.get("name")
            if row.get("is_secret") and row.get("value") in ("", "******") and name in existing:
                row = {**row, "value": existing[name]}
            out.append(row)
        return out
