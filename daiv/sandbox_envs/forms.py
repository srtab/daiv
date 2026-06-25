from __future__ import annotations

import json
import logging
import uuid

from django import forms
from django.utils.translation import gettext_lazy as _

from sandbox_envs.models import _ENV_VAR_NAME_RE, _REPO_ID_RE, ENV_VARS_MAX_ENTRIES, SandboxEnvironment, Scope

logger = logging.getLogger("daiv.sandbox_envs")

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
    egress_json = forms.CharField(required=False, widget=forms.HiddenInput())
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
        # Excludes ``is_default`` deliberately: the template renders no checkbox,
        # so including it here would demote the default on save (missing
        # BooleanField → ``False``). Promotion is handled by a dedicated view.
        fields = ("name", "description", "scope", "base_image", "cpus")
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, user=None, is_admin=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.is_admin = is_admin
        if not is_admin:
            # Non-admins cannot select GLOBAL. We restrict the available choices
            # rather than disabling the field, so a submitted GLOBAL value is
            # rejected by clean_scope / choice validation instead of being
            # silently coerced to the initial value.
            self.fields["scope"].choices = [(Scope.USER.value, Scope.USER.label)]
            self.fields["scope"].initial = Scope.USER
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
        self.fields["env_vars_json"].initial = self._initial_env_vars_json()
        self.fields["repo_ids_json"].initial = self._initial_repo_ids_json()

    def _initial_env_vars_json(self) -> str:
        """JSON string for the env-vars editor's initial state. Secret values
        are masked (rendered as ``""`` with a ``has_existing_value`` UI hint)
        so decrypted secrets never leak into page HTML. Returns ``"[]"`` for
        unsaved instances or when existing ciphertext cannot be decrypted —
        :meth:`_preserve_unchanged_secrets` still blocks save with a clear
        error on the POST path."""
        from core.encryption import DecryptionError

        if self.instance.pk is None:
            return "[]"
        try:
            rows = self.instance.env_vars or []
        except DecryptionError:
            logger.error(
                "env_vars decryption failed for SandboxEnvironment id=%s; rendering empty editor", self.instance.id
            )
            return "[]"
        masked = [
            {
                "name": r.get("name", ""),
                "value": "" if r.get("is_secret") else r.get("value", ""),
                "is_secret": bool(r.get("is_secret")),
                "has_existing_value": bool(r.get("is_secret")),
            }
            for r in rows
        ]
        return json.dumps(masked)

    def _initial_repo_ids_json(self) -> str:
        if self.instance.pk is None:
            return "[]"
        return json.dumps(list(self.instance.repo_ids or []))

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
            if not _REPO_ID_RE.match(value):
                raise forms.ValidationError(
                    _(
                        "Invalid repo id '%(value)s' at index %(idx)d. Use a slash-separated path "
                        "like 'owner/repo' or 'group/subgroup/repo'."
                    )
                    % {"value": value, "idx": idx}
                )
            cleaned.append(value)
        return cleaned

    def clean_egress_json(self):
        raw = (self.cleaned_data.get("egress_json") or "").strip()
        if not raw:
            return {"default": "deny", "intercept": "all", "hosts": []}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as err:
            raise forms.ValidationError(_("Egress policy must be valid JSON.")) from err
        if not isinstance(parsed, dict):
            raise forms.ValidationError(_("Egress policy must be an object."))
        default = parsed.get("default", "deny")
        intercept = parsed.get("intercept", "all")
        if default not in ("deny", "allow"):
            raise forms.ValidationError(_("Egress default must be 'deny' or 'allow'."))
        if intercept not in ("all", "credentialed"):
            raise forms.ValidationError(_("Egress intercept must be 'all' or 'credentialed'."))
        hosts_raw = parsed.get("hosts")
        if not isinstance(hosts_raw, list):
            raise forms.ValidationError(_("Egress hosts must be a list."))
        hosts: list[dict] = []
        for idx, entry in enumerate(hosts_raw):
            if not isinstance(entry, dict):
                raise forms.ValidationError(_("Egress host at index %d must be an object.") % idx)
            host = (entry.get("host") or "").strip()
            if not host:
                raise forms.ValidationError(_("Egress host at index %d cannot be blank.") % idx)
            methods = entry.get("methods")
            if isinstance(methods, list):
                methods = [str(m).strip().upper() for m in methods if str(m).strip()]
            if not methods:
                methods = ["*"]
            header = (entry.get("header") or "").strip()
            hosts.append({
                "host": host,
                "methods": methods,
                "header": header,
                "value": entry.get("value", ""),
                "secret_name": (entry.get("secret_name") or "").strip(),
                "has_existing_value": bool(entry.get("has_existing_value")),
                "has_credential": bool(header),
            })
        return {"default": default, "intercept": intercept, "hosts": hosts}

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
        egress_parsed = cleaned.get("egress_json") or {"default": "deny", "intercept": "all", "hosts": []}
        cleaned["egress_policy"], cleaned["egress_secrets"] = self._build_egress(egress_parsed)
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
        instance.egress_policy = self.cleaned_data.get("egress_policy")
        instance.egress_secrets = self.cleaned_data.get("egress_secrets") or {}
        if commit:
            instance.full_clean()
            instance.save()
        return instance

    @staticmethod
    def _build_egress(parsed: dict) -> tuple[dict, dict]:
        """Translate the normalised egress editor state into the stored
        ``(egress_policy, egress_secrets)`` shapes. A host with a credential
        synthesises a named secret (``inject``); hosts without one get
        ``inject=None``. Always returns an explicit policy (never None)."""
        rules: list[dict] = []
        secrets: dict[str, dict] = {}
        for host in parsed["hosts"]:
            inject = None
            if host["has_credential"]:
                name = host["secret_name"] or f"s_{uuid.uuid4().hex}"
                secrets[name] = {"header": host["header"], "value": host["value"]}
                inject = name
            rules.append({"host": host["host"], "methods": host["methods"], "inject": inject})
        policy = {"default": parsed["default"], "intercept": parsed["intercept"], "rules": rules}
        return policy, secrets

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
