from __future__ import annotations

import json

from django import forms
from django.utils.translation import gettext_lazy as _

from sandbox_envs.models import _ENV_VAR_NAME_RE, ENV_VARS_MAX_ENTRIES, SandboxEnvironment, Scope

MIB = 2**20
GIB = 2**30
_MEMORY_UNITS = {"MiB": MIB, "GiB": GIB}
_NETWORK_TO_CHOICE = {None: "default", True: "on", False: "off"}
_CHOICE_TO_NETWORK = {"default": None, "on": True, "off": False}


class SandboxEnvironmentForm(forms.ModelForm):
    """Form for creating/editing a SandboxEnvironment.

    * Non-admins cannot select scope=GLOBAL.
    * When editing the GLOBAL default, env-locked fields are read-only and pre-filled
      from ``site_settings``; submitted values for those fields are ignored.
    """

    env_vars_json = forms.CharField(required=False, widget=forms.HiddenInput())
    repo_ids_json = forms.CharField(required=False, widget=forms.HiddenInput())
    memory_value = forms.IntegerField(required=False, min_value=1)
    memory_unit = forms.ChoiceField(required=False, choices=[("MiB", "MiB"), ("GiB", "GiB")], initial="MiB")
    network_choice = forms.ChoiceField(
        required=False, choices=[("default", _("Use default")), ("on", _("On")), ("off", _("Off"))], initial="default"
    )
    memory_mode = forms.ChoiceField(
        required=False, choices=[("default", "default"), ("custom", "custom")], initial="default"
    )
    cpu_mode = forms.ChoiceField(
        required=False, choices=[("default", "default"), ("custom", "custom")], initial="default"
    )

    class Meta:
        model = SandboxEnvironment
        fields = ("name", "description", "scope", "base_image", "cpus", "is_default")
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}

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
        instance = self.instance
        if instance.pk is not None:
            self.initial.setdefault("network_choice", _NETWORK_TO_CHOICE[instance.network_enabled])
            if instance.memory_bytes:
                if instance.memory_bytes % GIB == 0:
                    self.initial.setdefault("memory_value", instance.memory_bytes // GIB)
                    self.initial.setdefault("memory_unit", "GiB")
                else:
                    self.initial.setdefault("memory_value", instance.memory_bytes // MIB)
                    self.initial.setdefault("memory_unit", "MiB")
            self.initial.setdefault("memory_mode", "custom" if instance.memory_bytes else "default")
            self.initial.setdefault("cpu_mode", "custom" if instance.cpus else "default")

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

    def clean_repo_ids_json(self):
        raw = (self.cleaned_data.get("repo_ids_json") or "").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as err:
            raise forms.ValidationError(_("repo_ids must be valid JSON.")) from err
        if not isinstance(parsed, list):
            raise forms.ValidationError(_("repo_ids must be a list."))
        cleaned: list[str] = []
        for idx, entry in enumerate(parsed):
            if not isinstance(entry, str):
                raise forms.ValidationError(_("repo_ids[%d] must be a string.") % idx)
            value = entry.strip()
            if not value:
                raise forms.ValidationError(_("repo_ids[%d] cannot be blank.") % idx)
            cleaned.append(value)
        return cleaned

    def clean(self):
        cleaned = super().clean()
        cleaned["network_enabled"] = _CHOICE_TO_NETWORK[cleaned.get("network_choice") or "default"]
        mv = cleaned.get("memory_value")
        mu = cleaned.get("memory_unit") or "MiB"
        cleaned["memory_bytes"] = mv * _MEMORY_UNITS[mu] if mv else None
        if cleaned.get("memory_mode") == "custom" and mv is None:
            self.add_error("memory_value", _("Enter a memory value or switch back to default."))
        if cleaned.get("cpu_mode") == "custom" and cleaned.get("cpus") is None:
            self.add_error("cpus", _("Enter a CPU value or switch back to default."))
        return cleaned

    def save(self, commit: bool = True) -> SandboxEnvironment:
        env_vars = self.cleaned_data.get("env_vars_json") or []
        instance: SandboxEnvironment = super().save(commit=False)
        instance.network_enabled = self.cleaned_data.get("network_enabled")
        instance.memory_bytes = self.cleaned_data.get("memory_bytes")
        instance.repo_ids = self.cleaned_data.get("repo_ids_json") or []
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
