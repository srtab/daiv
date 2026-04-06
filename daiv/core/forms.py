from __future__ import annotations

from typing import Any, ClassVar

from django import forms
from django.utils.translation import gettext_lazy as _

from automation.agent.constants import ModelName
from core.encryption import encrypt_value
from core.models import SiteConfiguration

# Datalist choices for model name fields (suggestions, not enforced)
MODEL_NAME_CHOICES = [(m.value, m.value) for m in ModelName]


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


class SiteConfigurationForm(forms.ModelForm):
    """
    Auto-generated form for :class:`~core.models.SiteConfiguration`.

    Widgets and field template names are derived from the model field type.
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
                widget.attrs["list"] = "model-names"
            if isinstance(widget, forms.NumberInput):
                widget.attrs.setdefault("min", "0")

            # Set template and default attributes on remaining non-secret fields
            field_obj.template_name = "core/fields/default.html"  # type: ignore[attr-defined]
            field_obj.is_env_locked = False  # type: ignore[attr-defined]
            field_obj.secret_hint = None  # type: ignore[attr-defined]

    def _apply_env_locks(self) -> None:
        """Disable fields locked by environment variables and set effective initial values for locked checkboxes."""
        from core.site_settings import site_settings

        for name in self.env_locked_fields:
            if name not in self.fields:
                continue
            field_obj = self.fields[name]
            field_obj.disabled = True
            field_obj.is_env_locked = True  # type: ignore[attr-defined]
            field_obj.widget.attrs["title"] = _("Locked by environment variable")
            if isinstance(field_obj.widget, forms.CheckboxInput):
                # When a boolean field is locked by an env var the DB typically
                # holds NULL, so the checkbox would appear unchecked without this.
                self.initial[name] = bool(getattr(site_settings, name, False))

    def _apply_defaults(self, field_defaults: dict[str, str]) -> None:
        """Set effective defaults as placeholders, empty-choice labels, or checkbox initial values."""
        for name, default_str in field_defaults.items():
            if name not in self.fields or name in self.env_locked_fields:
                continue
            field_obj = self.fields[name]
            widget = field_obj.widget
            if isinstance(widget, (forms.TextInput, forms.NumberInput)):
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

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(self, commit: bool = True) -> SiteConfiguration:
        instance = super().save(commit=False)

        for field_name in self.SECRET_FIELDS:
            if field_name in self.env_locked_fields:
                continue
            encrypted_column = f"_{field_name}_encrypted"
            if field_name in self.cleared_secrets:
                setattr(instance, encrypted_column, None)
            else:
                value = self.cleaned_data.get(field_name, "")
                if value:
                    setattr(instance, encrypted_column, encrypt_value(value))

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
        }
        return labels.get(name, "")
