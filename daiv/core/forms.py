from __future__ import annotations

from typing import Any, ClassVar

from django import forms
from django.utils.translation import gettext_lazy as _

from automation.agent.base import ModelProvider, parse_model_spec
from automation.agent.constants import MODEL_SUGGESTIONS
from core.models import SiteConfiguration


class _BooleanCheckboxField(forms.BooleanField):
    """BooleanField for nullable model fields. Checked=True, unchecked=False."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("required", False)
        super().__init__(*args, **kwargs)
        self.template_name = "core/fields/checkbox.html"


class _SecretFormField(forms.CharField):
    """CharField for encrypted secret fields."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.template_name = "core/fields/secret.html"


class _ModelSpecWidget(forms.Widget):
    """
    Composite widget that renders a provider ``<select>`` and a model name
    ``<input>`` side by side. The two parts are stored as a single
    ``provider:model_name`` string in the database.
    """

    template_name = "core/fields/model_spec_widget.html"

    def __init__(self, *, default_provider: str = "openrouter", attrs: dict | None = None):
        super().__init__(attrs)
        self.default_provider = default_provider

    def get_context(self, name: str, value: Any, attrs: dict | None) -> dict[str, Any]:
        context = super().get_context(name, value, attrs)
        if value:
            try:
                provider, model_name = parse_model_spec(value)
                context["widget"]["provider"] = provider.value
                context["widget"]["model_name"] = model_name
            except ValueError:
                context["widget"]["provider"] = ""
                context["widget"]["model_name"] = value
        else:
            context["widget"]["provider"] = ""
            context["widget"]["model_name"] = ""
        context["widget"]["default_provider"] = self.default_provider
        context["widget"]["providers"] = [
            ("", self.default_provider_label),
            *((p.value, p.value.replace("_", " ").title()) for p in ModelProvider),
        ]
        context["widget"]["model_suggestions"] = MODEL_SUGGESTIONS
        return context

    @property
    def default_provider_label(self) -> str:
        """Label for the empty/default provider option."""
        label = self.default_provider.replace("_", " ").title()
        return f"Default ({label})"

    def value_from_datadict(self, data: dict[str, Any], files: dict[str, Any], name: str) -> str:
        provider = data.get(f"{name}_provider", "").strip()
        model_name = data.get(f"{name}_model", "").strip()
        if not model_name:
            return ""
        effective_provider = provider or self.default_provider
        return f"{effective_provider}:{model_name}"


class SiteConfigurationForm(forms.ModelForm):
    """
    ModelForm for :class:`~core.models.SiteConfiguration` with custom handling
    for encrypted secrets, nullable booleans, and environment-locked fields.

    Base CSS styles for inputs, selects, and checkboxes are centralised
    in the Tailwind source (``input.css``), so no per-field CSS class
    assignment is needed here.
    """

    SECRET_FIELDS: ClassVar[tuple[str, ...]] = SiteConfiguration.ENCRYPTED_FIELDS

    class Meta:
        model = SiteConfiguration
        fields = [
            "agent_model_name",
            "agent_fallback_model_name",
            "agent_thinking_level",
            "agent_max_model_name",
            "agent_max_thinking_level",
            "agent_explore_model_name",
            "agent_explore_fallback_model_name",
            "agent_recursion_limit",
            "diff_to_metadata_model_name",
            "diff_to_metadata_fallback_model_name",
            "web_search_enabled",
            "web_search_max_results",
            "web_search_engine",
            "web_fetch_enabled",
            "web_fetch_model_name",
            "web_fetch_cache_ttl_seconds",
            "web_fetch_timeout_seconds",
            "web_fetch_max_content_chars",
            "sandbox_timeout",
            "sandbox_base_image",
            "sandbox_ephemeral",
            "sandbox_network_enabled",
            "sandbox_cpu",
            "sandbox_memory",
            "suggest_context_file_enabled",
            "openrouter_api_base",
            "jobs_throttle_rate",
            "auth_login_enabled",
            "auth_client_id",
            "auth_gitlab_url",
            "auth_gitlab_server_url",
        ]

    def __init__(
        self,
        *args: Any,
        env_locked_fields: set[str] | None = None,
        cleared_secrets: set[str] | None = None,
        field_defaults: dict[str, str] | None = None,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.env_locked_fields = env_locked_fields or set()
        self.cleared_secrets = cleared_secrets or set()

        self._add_secret_fields()
        self._configure_widgets()
        # Order matters: _apply_env_locks sets checkbox initial values for
        # env-locked fields; _apply_defaults must run after and skip them.
        self._apply_env_locks()
        self._apply_defaults(field_defaults or {})
        self._hide_inapplicable_auth_fields()

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    def _add_secret_fields(self) -> None:
        """Add virtual password fields for each encrypted field."""
        for name in self.SECRET_FIELDS:
            field_obj = _SecretFormField(
                label=self._secret_label(name),
                required=False,
                widget=forms.PasswordInput(attrs={"autocomplete": "off"}),
                help_text=self._secret_help_text(name),
            )
            field_obj.secret_hint = self.instance.get_secret_hint(name) if self.instance else None  # type: ignore[attr-defined]
            field_obj.is_env_locked = name in self.env_locked_fields  # type: ignore[attr-defined]
            self.fields[name] = field_obj

    def _configure_widgets(self) -> None:
        """Configure widgets, replace nullable booleans with checkbox fields, and set default field attributes."""
        from django.core.exceptions import FieldDoesNotExist
        from django.db import models

        for name, field_obj in list(self.fields.items()):
            if name in self.SECRET_FIELDS:
                continue  # already configured in _add_secret_fields

            # Replace Django's default NullBooleanSelect (Unknown/Yes/No dropdown)
            # with a standard BooleanField+CheckboxInput so checked=True, unchecked=False.
            try:
                model_field = SiteConfiguration._meta.get_field(name)
            except FieldDoesNotExist:
                model_field = None
            if isinstance(model_field, models.BooleanField) and model_field.null:
                new_field = _BooleanCheckboxField(label=field_obj.label, help_text=field_obj.help_text)
                new_field.is_env_locked = False  # type: ignore[attr-defined]
                new_field.secret_hint = None  # type: ignore[attr-defined]
                self.fields[name] = new_field
                continue

            widget = field_obj.widget
            if isinstance(widget, (forms.TextInput, forms.NumberInput)) and "model_name" in name:
                field_obj.widget = _ModelSpecWidget()
            elif isinstance(widget, forms.NumberInput):
                widget.attrs.setdefault("min", "0")

            # Set template and default attributes on remaining non-secret fields
            field_obj.template_name = "core/fields/default.html"  # type: ignore[attr-defined]
            field_obj.is_env_locked = False  # type: ignore[attr-defined]
            field_obj.secret_hint = None  # type: ignore[attr-defined]

    def _apply_env_locks(self) -> None:
        """Disable fields locked by environment variables and set effective initial values."""
        from core.encryption import mask_secret
        from core.site_settings import site_settings

        for name in self.env_locked_fields:
            if name not in self.fields:
                continue
            field_obj = self.fields[name]
            field_obj.disabled = True
            field_obj.is_env_locked = True  # type: ignore[attr-defined]
            field_obj.widget.attrs["title"] = _("Locked by environment variable")
            # When a field is locked by an env var the DB typically holds NULL,
            # so the widget would appear empty without setting the effective value.
            effective = getattr(site_settings, name, None)
            if isinstance(field_obj.widget, forms.CheckboxInput):
                self.initial[name] = bool(effective)
            elif effective is not None:
                self.initial[name] = effective
            # Env-locked secrets have NULL in the DB, so get_secret_hint returns
            # nothing. Generate a hint from the env value so the UI shows the field is set.
            if name in self.SECRET_FIELDS and effective is not None and not getattr(field_obj, "secret_hint", None):
                raw = effective.get_secret_value() if hasattr(effective, "get_secret_value") else str(effective)
                field_obj.secret_hint = mask_secret(raw)  # type: ignore[attr-defined]

    def _apply_defaults(self, field_defaults: dict[str, str]) -> None:
        """Set effective defaults as placeholders, empty-choice labels, or checkbox initial values."""
        for name, default_str in field_defaults.items():
            if name not in self.fields or name in self.env_locked_fields:
                continue
            field_obj = self.fields[name]
            widget = field_obj.widget
            if isinstance(widget, _ModelSpecWidget):
                try:
                    provider, model_name = parse_model_spec(default_str)
                    widget.default_provider = provider.value
                    widget.attrs.setdefault("placeholder", model_name)
                except ValueError:
                    widget.attrs.setdefault("placeholder", default_str)
            elif isinstance(widget, (forms.TextInput, forms.NumberInput)):
                widget.attrs.setdefault("placeholder", default_str)
            elif isinstance(widget, forms.Select) and hasattr(field_obj, "choices"):
                choices = list(field_obj.choices)
                if choices and choices[0][0] == "":
                    choices[0] = ("", f"Default ({default_str})")
                    field_obj.choices = choices
            elif (
                isinstance(widget, forms.CheckboxInput) and self.instance and getattr(self.instance, name, None) is None
            ):
                # When DB value is NULL, show the checkbox with the default state
                self.initial[name] = default_str.lower() in ("true", "1", "yes", "on")

    def _hide_inapplicable_auth_fields(self) -> None:
        """Remove auth fields that don't apply to the current CODEBASE_CLIENT."""
        from codebase.base import GitPlatform
        from codebase.conf import settings as codebase_settings

        client = codebase_settings.CLIENT
        if client == GitPlatform.GITHUB:
            for name in ("auth_gitlab_url", "auth_gitlab_server_url"):
                self.fields.pop(name, None)
        elif client != GitPlatform.GITLAB:
            for name in (
                "auth_login_enabled",
                "auth_client_id",
                "auth_client_secret",
                "auth_gitlab_url",
                "auth_gitlab_server_url",
            ):
                self.fields.pop(name, None)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def clean(self) -> dict[str, Any]:
        cleaned_data = super().clean()
        self._validate_model_api_keys(cleaned_data)
        self._validate_web_search_api_key(cleaned_data)
        self._validate_auth_credentials(cleaned_data)
        return cleaned_data

    def _validate_model_api_keys(self, cleaned_data: dict[str, Any]) -> None:
        """Validate that each chosen model has an API key for its provider."""
        from automation.agent.base import BaseAgent, ModelProvider

        for field_name in SiteConfiguration.MODEL_NAME_FIELDS:
            model_name = cleaned_data.get(field_name)
            if not model_name:
                continue
            try:
                provider = BaseAgent.get_model_provider(model_name)
            except ValueError:
                self.add_error(field_name, _("Unsupported model: %(model)s.") % {"model": model_name})
                continue
            key_field = ModelProvider.api_key_field_for(provider)
            if key_field and not self._has_api_key(key_field, cleaned_data):
                self.add_error(
                    field_name,
                    _("No API key for %(provider)s. Set the %(key_label)s below or via environment variable.")
                    % {"provider": provider.value.replace("_", " ").title(), "key_label": key_field.replace("_", " ")},
                )

    def _validate_web_search_api_key(self, cleaned_data: dict[str, Any]) -> None:
        """Validate that Tavily has an API key when selected."""
        from core.models import WebSearchEngineChoices

        if cleaned_data.get("web_search_engine") == WebSearchEngineChoices.TAVILY and not self._has_api_key(
            "web_search_api_key", cleaned_data
        ):
            self.add_error(
                "web_search_engine",
                _("Tavily requires an API key. Set the web search API key below or via environment variable."),
            )

    def _validate_auth_credentials(self, cleaned_data: dict[str, Any]) -> None:
        """Validate that OAuth client ID and secret are configured as a pair."""
        if "auth_client_id" not in self.fields:
            return
        has_client_id = bool(cleaned_data.get("auth_client_id")) or bool(self.instance and self.instance.auth_client_id)
        has_secret = self._has_api_key("auth_client_secret", cleaned_data)
        if has_client_id and not has_secret:
            self.add_error("auth_client_secret", _("OAuth requires both a client ID and a client secret."))
        elif has_secret and not has_client_id:
            self.add_error("auth_client_id", _("OAuth requires both a client ID and a client secret."))

    def _has_api_key(self, field_name: str, cleaned_data: dict[str, Any]) -> bool:
        """Check if an API key is available via env var, form submission, or existing DB value."""
        if field_name in self.env_locked_fields:
            return True
        if cleaned_data.get(field_name):
            return True
        if field_name in self.cleared_secrets:
            return False
        return bool(self.instance and self.instance.get_secret_hint(field_name))

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(self, commit: bool = True) -> SiteConfiguration:
        instance = super().save(commit=False)

        for field_name in self.SECRET_FIELDS:
            if field_name in self.env_locked_fields:
                continue
            if field_name in self.cleared_secrets:
                setattr(instance, field_name, None)
            else:
                value = self.cleaned_data.get(field_name, "")
                if value:
                    setattr(instance, field_name, value)

        if commit:
            instance.save()
        return instance

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _secret_label(name: str) -> str:
        return name.replace("_", " ").replace("api key", "API key").title().replace("Api Key", "API key")

    @staticmethod
    def _secret_help_text(name: str) -> str:
        labels: dict[str, str] = {
            "anthropic_api_key": _("API key for Anthropic models."),
            "openai_api_key": _("API key for OpenAI models."),
            "google_api_key": _("API key for Google AI models."),
            "openrouter_api_key": _("API key for OpenRouter."),
            "web_search_api_key": _("API key for Tavily web search engine."),
            "sandbox_api_key": _("API key for the sandbox service."),
            "auth_client_secret": _("OAuth application client secret for the configured Git platform."),
        }
        return labels.get(name, "")
