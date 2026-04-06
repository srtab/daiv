from __future__ import annotations

from typing import Any, ClassVar

from django import forms
from django.utils.translation import gettext_lazy as _

from automation.agent.constants import ModelName
from core.encryption import encrypt_value
from core.models import SiteConfiguration

# Datalist choices for model name fields (suggestions, not enforced)
MODEL_NAME_CHOICES = [(m.value, m.value) for m in ModelName]

# Semantic CSS class names — the actual Tailwind utilities are referenced in field templates.
_INPUT_CSS = (
    "block w-full rounded-xl border border-white/[0.06] bg-white/[0.03] px-4 py-2.5 "
    "text-[14px] text-white placeholder-gray-500 outline-none transition-all duration-200 "
    "focus:border-white/[0.15] focus:bg-white/[0.05] focus:ring-1 focus:ring-white/[0.08]"
)
_DISABLED_CSS = f"{_INPUT_CSS} opacity-50 cursor-not-allowed"
_SELECT_CSS = _INPUT_CSS
_CHECKBOX_CSS = "h-4 w-4 rounded border-white/[0.15] bg-white/[0.03] text-white accent-white"


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

    Widgets, CSS classes, and field template names are derived from the model
    field type — no per-field widget declaration needed.
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
                widget=forms.PasswordInput(attrs={"class": _INPUT_CSS, "autocomplete": "off"}),
                help_text=self._secret_help_text(name),
            )
            field_obj.secret_hint = self.instance.get_secret_hint(name) if self.instance else None  # type: ignore[attr-defined]
            field_obj.is_env_locked = name in self.env_locked_fields  # type: ignore[attr-defined]
            self.fields[name] = field_obj

    def _configure_widgets(self) -> None:
        """Auto-configure widget attrs based on model field type."""
        from django.core.exceptions import FieldDoesNotExist
        from django.db import models

        for name, field_obj in list(self.fields.items()):
            if name in self.SECRET_FIELDS:
                continue  # already configured in _add_secret_fields

            # Replace Django's auto-generated NullBooleanField with a standard
            # BooleanField+CheckboxInput so checked=True, unchecked=False.
            try:
                model_field = SiteConfiguration._meta.get_field(name)
            except FieldDoesNotExist:
                model_field = None
            if isinstance(model_field, models.BooleanField) and model_field.null:
                new_field = _BooleanCheckboxField(
                    widget=forms.CheckboxInput(attrs={"class": _CHECKBOX_CSS}),
                    label=field_obj.label,
                    help_text=field_obj.help_text,
                )
                new_field.is_env_locked = False  # type: ignore[attr-defined]
                new_field.secret_hint = None  # type: ignore[attr-defined]
                self.fields[name] = new_field
                continue

            widget = field_obj.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", _CHECKBOX_CSS)
            elif isinstance(widget, forms.Select):
                widget.attrs.setdefault("class", _SELECT_CSS)
            elif isinstance(widget, (forms.TextInput, forms.NumberInput)):
                widget.attrs.setdefault("class", _INPUT_CSS)
                if "model_name" in name:
                    widget.attrs["list"] = "model-names"
            if isinstance(widget, forms.NumberInput):
                widget.attrs.setdefault("min", "0")

            # Set template and default attributes on remaining non-secret fields
            field_obj.template_name = "core/fields/default.html"  # type: ignore[attr-defined]
            if not hasattr(field_obj, "is_env_locked"):
                field_obj.is_env_locked = False  # type: ignore[attr-defined]
            if not hasattr(field_obj, "secret_hint"):
                field_obj.secret_hint = None  # type: ignore[attr-defined]

    def _apply_env_locks(self) -> None:
        """Disable fields locked by environment variables."""
        for name in self.env_locked_fields:
            if name not in self.fields:
                continue
            field_obj = self.fields[name]
            field_obj.disabled = True
            field_obj.is_env_locked = True  # type: ignore[attr-defined]
            widget = field_obj.widget
            if hasattr(widget, "attrs"):
                widget.attrs["class"] = _DISABLED_CSS
                widget.attrs["title"] = _("Locked by environment variable")

    def _apply_defaults(self, field_defaults: dict[str, str]) -> None:
        """Set effective defaults as placeholders / empty-choice labels."""
        for name, default_str in field_defaults.items():
            if name not in self.fields:
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
