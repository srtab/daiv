from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from django import forms
from django.forms.models import BaseModelFormSet, modelformset_factory
from django.utils.translation import gettext_lazy as _

from automation.agent.base import parse_model_spec
from core.models import Provider, SiteConfiguration, WebFetchAuthHeader

if TYPE_CHECKING:
    from core.models import FieldGroup


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
                resolved = parse_model_spec(value)
                context["widget"]["provider"] = resolved.row.slug
                context["widget"]["model_name"] = resolved.model_name
            except ValueError:
                context["widget"]["provider"] = ""
                context["widget"]["model_name"] = value
        else:
            context["widget"]["provider"] = ""
            context["widget"]["model_name"] = ""
        rows = Provider.get_cached_rows()
        context["widget"]["providers"] = [
            ("", self.default_provider_label),
            *[(r.slug, f"{r.display_name}{'' if r.is_enabled else ' (disabled)'}") for r in rows if r.is_enabled],
        ]
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
            "titling_model_name",
            "titling_fallback_model_name",
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
            "rocketchat_enabled",
            "rocketchat_url",
            "rocketchat_user_id",
            "jobs_throttle_rate",
            "auth_login_enabled",
            "auth_signup_open",
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
        in_flight_providers: dict[str, tuple[bool, bool]] | None = None,
        group: FieldGroup | None = None,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.env_locked_fields = env_locked_fields or set()
        self.cleared_secrets = cleared_secrets or set()
        # Maps provider slug → (is_enabled, has_key) for the in-flight providers
        # formset. When set, ``_validate_model_api_keys`` consults this map
        # first per slug and only falls back to the cached ``Provider`` rows
        # for slugs not present — so a single POST that enables a provider and
        # selects one of its models passes validation.
        self.in_flight_providers = in_flight_providers
        self.group = group

        self._add_secret_fields()
        self._configure_widgets()
        # Order matters: _apply_env_locks sets checkbox initial values for
        # env-locked fields; _apply_defaults must run after and skip them.
        self._apply_env_locks()
        self._apply_defaults(field_defaults or {})
        self._hide_inapplicable_auth_fields()
        self._restrict_to_group()

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
                    resolved = parse_model_spec(default_str)
                    widget.default_provider = resolved.row.slug
                    widget.attrs.setdefault("placeholder", resolved.model_name)
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
                "auth_signup_open",
                "auth_client_id",
                "auth_client_secret",
                "auth_gitlab_url",
                "auth_gitlab_server_url",
            ):
                self.fields.pop(name, None)

    def _restrict_to_group(self) -> None:
        """When ``group`` is set, drop any field not in ``group.fields``."""
        if self.group is None:
            return
        allowed = set(self.group.fields)
        for name in list(self.fields):
            if name not in allowed:
                del self.fields[name]

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
        """Validate that each chosen model resolves to an enabled, keyed Provider row."""
        rows = Provider.get_cached_by_slug()
        for field_name in SiteConfiguration.MODEL_NAME_FIELDS:
            model_spec = cleaned_data.get(field_name)
            if not model_spec:
                continue
            slug = self._resolve_provider_slug(model_spec, rows)
            if slug is None:
                self.add_error(field_name, _("Unsupported model: %(m)s.") % {"m": model_spec})
                continue
            if self.in_flight_providers is not None and slug in self.in_flight_providers:
                is_enabled, has_key = self.in_flight_providers[slug]
            elif (row := rows.get(slug)) is not None:
                is_enabled, has_key = row.is_enabled, row.api_key is not None
            else:
                self.add_error(field_name, _("Provider '%(s)s' is not configured.") % {"s": slug})
                continue
            if not is_enabled:
                self.add_error(
                    field_name, _("Provider '%(s)s' is disabled. Enable it in the Providers section.") % {"s": slug}
                )
                continue
            if not has_key:
                self.add_error(
                    field_name, _("Provider '%(s)s' has no API key. Set it in the Providers section.") % {"s": slug}
                )

    def _resolve_provider_slug(self, model_spec: str, rows: dict) -> str | None:
        """Return the provider slug for ``model_spec`` or ``None`` if unparseable.

        Falls back to the literal prefix when ``parse_model_spec`` fails because
        the slug references an in-flight provider not yet in the cached rows.
        """
        try:
            return parse_model_spec(model_spec).row.slug
        except ValueError:
            if ":" not in model_spec:
                return None
            prefix, model_name = model_spec.split(":", 1)
            if not model_name.strip() or not prefix:
                return None
            if self.in_flight_providers is not None and prefix in self.in_flight_providers:
                return prefix
            return None

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
            "web_search_api_key": _("API key for Tavily web search engine."),
            "sandbox_api_key": _("API key for the sandbox service."),
            "auth_client_secret": _("OAuth application client secret for the configured Git platform."),
            "rocketchat_auth_token": _("Bot user personal access token (X-Auth-Token)."),
        }
        return labels.get(name, "")


WEB_FETCH_AUTH_HEADERS_FORMSET_PREFIX = "headers"

_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]+$")


class WebFetchAuthHeaderForm(forms.ModelForm):
    """
    Form for one row of the web_fetch auth-headers formset.

    The ``header_value`` field is rendered as a password input and reuses
    the existing secret-field template for masked-hint UX.
    """

    header_value = _SecretFormField(
        label=_("header value"), required=False, widget=forms.PasswordInput(attrs={"autocomplete": "off"})
    )

    class Meta:
        model = WebFetchAuthHeader
        fields = ("domain", "header_name", "header_value")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["domain"].widget.attrs.update({"placeholder": "example.com", "aria-label": _("Domain")})
        self.fields["header_name"].widget.attrs.update({"placeholder": "X-API-Key", "aria-label": _("Header name")})
        self.fields["header_value"].widget.attrs.update({"aria-label": _("Header value")})
        hint = self.instance.get_secret_hint() if self.instance and self.instance.pk else None
        self.fields["header_value"].secret_hint = hint  # type: ignore[attr-defined]

    def has_changed(self) -> bool:
        if self.empty_permitted and not any(
            (self[name].value() or "").strip() for name in ("domain", "header_name", "header_value")
        ):
            return False
        return super().has_changed()

    def clean_domain(self) -> str:
        value = (self.cleaned_data.get("domain") or "").strip().lower()
        if not value:
            return value
        if "://" in value or "/" in value or "?" in value or "#" in value or " " in value:
            raise forms.ValidationError(_("Enter a host only (e.g. example.com)."))
        parsed = urlparse(f"https://{value}")
        if parsed.hostname != value or parsed.port is not None:
            raise forms.ValidationError(_("Enter a host only (e.g. example.com)."))
        return value

    def clean_header_name(self) -> str:
        value = (self.cleaned_data.get("header_name") or "").strip()
        if value and not _HEADER_NAME_RE.match(value):
            raise forms.ValidationError(_("Use letters, digits, and hyphens only (e.g. X-API-Key)."))
        return value

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        keeping_existing_value = bool(self.instance and self.instance.pk and not cleaned.get("header_value"))
        present = {
            "domain": bool(cleaned.get("domain")),
            "header_name": bool(cleaned.get("header_name")),
            "header_value": bool(cleaned.get("header_value")) or keeping_existing_value,
        }
        if any(present.values()) and not all(present.values()):
            for field, is_present in present.items():
                if not is_present:
                    self.add_error(field, _("Required."))
        return cleaned

    def save(self, commit: bool = True) -> WebFetchAuthHeader:
        instance = super().save(commit=False)
        new_value = self.cleaned_data.get("header_value")
        if new_value:
            instance.header_value = new_value
        if commit:
            instance.save()
        return instance


class _WebFetchAuthHeaderFormset(BaseModelFormSet):
    def clean(self) -> None:
        super().clean()
        seen: set[tuple[str, str]] = set()
        for form in self.forms:
            if not form.cleaned_data or form.cleaned_data.get("DELETE"):
                continue
            pair = (form.cleaned_data.get("domain", ""), form.cleaned_data.get("header_name", ""))
            if not pair[0] or not pair[1]:
                continue
            if pair in seen:
                raise forms.ValidationError(
                    _("Duplicate (domain, header name) pair: %(domain)s / %(header)s.")
                    % {"domain": pair[0], "header": pair[1]}
                )
            seen.add(pair)


def build_web_fetch_auth_header_formset():
    """
    Factory for the model formset. Wrapped in a function so each request
    instantiates a fresh class.
    """
    return modelformset_factory(
        WebFetchAuthHeader, form=WebFetchAuthHeaderForm, formset=_WebFetchAuthHeaderFormset, extra=0, can_delete=True
    )


PROVIDERS_FORMSET_PREFIX = "providers"
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class ProviderForm(forms.ModelForm):
    """One row in the providers formset.

    Mirrors :class:`WebFetchAuthHeaderForm` for the secret/secret-hint UX:
    blank ``api_key`` on an existing instance preserves the stored secret;
    submitting a new value rotates it.
    """

    api_key = _SecretFormField(
        label=_("API key"), required=False, widget=forms.PasswordInput(attrs={"autocomplete": "off"})
    )
    # Submitted only when the user clicks the per-row "Clear" button, which sets
    # the row's hidden ``providers-N-clear_api_key`` input to "on" before
    # submitting. The button is type="button" so a stray Enter keypress can't
    # activate it.
    clear_api_key = forms.BooleanField(required=False)
    extra_headers = forms.CharField(
        label=_("Extra headers (JSON)"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": '{"X-Foo": "bar"}'}),
    )

    class Meta:
        model = Provider
        fields = (
            "slug",
            "display_name",
            "provider_type",
            "base_url",
            "api_key",
            "extra_headers",
            "is_enabled",
            "sort_order",
        )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["api_key"].secret_hint = self.instance.get_secret_hint()  # type: ignore[attr-defined]
            if self.instance.is_locked:
                self.fields["slug"].disabled = True
                self.fields["provider_type"].disabled = True
        if self.instance and isinstance(self.instance.extra_headers, dict) and self.instance.extra_headers:
            self.initial.setdefault("extra_headers", json.dumps(self.instance.extra_headers))

    def clean_slug(self) -> str:
        value = (self.cleaned_data.get("slug") or "").strip()
        if value == "google":
            raise forms.ValidationError(_("'google' is a reserved alias."))
        if not _SLUG_RE.match(value):
            raise forms.ValidationError(
                _("Slug must start with a lowercase letter; lowercase letters, digits, '-' and '_'; max 32 chars.")
            )
        if self.instance and self.instance.pk and value != self.instance.slug:
            raise forms.ValidationError(_("Slug is immutable after creation; delete and re-add to change."))
        return value

    def clean_provider_type(self) -> str | None:
        value = self.cleaned_data.get("provider_type")
        if self.instance and self.instance.pk and self.instance.is_locked and value != self.instance.provider_type:
            raise forms.ValidationError(_("Locked provider; provider type cannot be changed."))
        return value

    def clean_base_url(self) -> str:
        value = (self.cleaned_data.get("base_url") or "").strip()
        if not value:
            return value
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https"):
            raise forms.ValidationError(_("Base URL must use http or https."))
        return value

    def clean_extra_headers(self) -> dict:
        raw = (self.cleaned_data.get("extra_headers") or "").strip()
        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as e:
            raise forms.ValidationError(_("Invalid JSON: %s") % e) from e
        if not isinstance(decoded, dict):
            raise forms.ValidationError(_("Extra headers must be a JSON object."))
        for name, value in decoded.items():
            if not isinstance(name, str) or not _HEADER_NAME_RE.match(name):
                raise forms.ValidationError(_("Invalid header name: %s") % name)
            if not isinstance(value, str):
                raise forms.ValidationError(_("Header value for %s must be a string.") % name)
        return decoded

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        # Clearing implies disabling; skip the "required when enabled" check so
        # the click doesn't dead-end on a validation error. Drop any submitted
        # ``api_key`` too — keeps ``save`` and downstream readers (e.g. the
        # in-flight providers map) symmetric on "the key is gone."
        if cleaned.get("clear_api_key"):
            cleaned["is_enabled"] = False
            cleaned["api_key"] = ""
            return cleaned
        keeping_existing = bool(
            self.instance and self.instance.pk and self.instance.api_key and not cleaned.get("api_key")
        )
        # A disabled row with no key is a legitimate state (the seed rows ship this
        # way before an admin configures them); only require api_key when the row
        # is enabled or newly created.
        is_enabled = cleaned.get("is_enabled", False)
        if is_enabled and not cleaned.get("api_key") and not keeping_existing:
            self.add_error("api_key", _("Required when enabled."))
        return cleaned

    def save(self, commit: bool = True) -> Provider:
        instance = super().save(commit=False)
        if self.cleaned_data.get("clear_api_key"):
            instance.api_key = None
            instance.is_enabled = False
        elif new_key := self.cleaned_data.get("api_key"):
            instance.api_key = new_key
        if commit:
            instance.save()
        return instance


class _ProviderFormset(BaseModelFormSet):
    def clean(self) -> None:
        super().clean()
        seen: set[str] = set()
        for form in self.forms:
            if not form.cleaned_data:
                continue
            if form.cleaned_data.get("DELETE"):
                # Reject delete on a locked seed row here. ``Provider.delete``
                # also raises ValueError, but that surfaces as a 500 on the
                # admin POST; surfacing it as a formset error keeps the
                # response a normal re-render with a visible message.
                if form.instance and form.instance.pk and form.instance.is_locked:
                    raise forms.ValidationError(_("Locked provider '%s' cannot be deleted.") % form.instance.slug)
                continue
            slug = form.cleaned_data.get("slug")
            if not slug:
                continue
            if slug in seen:
                raise forms.ValidationError(_("Duplicate slug: %s") % slug)
            seen.add(slug)


def build_provider_formset():
    """Factory for the Provider model formset."""
    return modelformset_factory(Provider, form=ProviderForm, formset=_ProviderFormset, extra=0, can_delete=True)
