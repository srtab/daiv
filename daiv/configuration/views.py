import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.contrib import messages
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django.views import View

from skills.services import list_global_skills

from configuration.forms import (
    PROVIDERS_FORMSET_PREFIX,
    SKILL_MODELS_FORMSET_PREFIX,
    SUBAGENT_MODELS_FORMSET_PREFIX,
    WEB_FETCH_AUTH_HEADERS_FORMSET_PREFIX,
    ModelMapKind,
    SiteConfigurationForm,
    build_model_map_formset,
    build_provider_formset,
    build_web_fetch_auth_header_formset,
    model_map_from_formset,
    model_map_names_id,
)
from core.mixins import AdminRequiredMixin
from core.models import FieldGroup, Provider, SiteConfiguration, WebFetchAuthHeader
from core.site_settings import site_settings

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger("daiv.configuration")


@dataclass(frozen=True)
class _ModelMapSection:
    """One model map of the Agent group: its rows, or the environment's value when a variable locks it."""

    kind: ModelMapKind
    field: str
    title: str
    add_label: str
    unknown_warning: str
    suggestions: list[str]
    formset: Any
    env_value: Mapping[str, str] | None

    @property
    def names_id(self) -> str:
        return model_map_names_id(self.kind)

    @property
    def saves(self) -> bool:
        return self.formset is not None and self.formset.is_bound

    def unknown_names(self) -> list[str]:
        return [name for name in model_map_from_formset(self.formset) if name not in self.suggestions]


class SiteConfigurationIndexView(AdminRequiredMixin, View):
    """Redirect admins to the first configuration group."""

    def get(self, request):
        first_group = SiteConfiguration.get_field_groups()[0]
        return redirect("site_configuration", group_key=first_group.key)


class SiteConfigurationGroupView(AdminRequiredMixin, View):
    """
    Admin-only view for managing one ``FieldGroup`` of the site-wide configuration.

    The active group is determined by the URL kwarg ``group_key``. Unknown keys
    raise ``Http404``. Saves are scoped to the group's fields only.
    """

    template_name = "configuration/site_configuration_group.html"

    group: FieldGroup

    def get(self, request, group_key):
        self.group = self._resolve_group(group_key)
        instance = SiteConfiguration.objects.get_instance()
        field_defaults = site_settings.get_defaults()
        providers_formset, headers_formset, headers_env_locked = self._build_per_group_formsets(data=None)
        model_maps = self._build_model_map_sections(data=None, instance=instance)
        form = SiteConfigurationForm(
            instance=instance,
            env_locked_fields=self._get_env_locked_fields(),
            field_defaults=field_defaults,
            group=self.group,
        )
        return render(
            request,
            self.template_name,
            self._build_context(form, providers_formset, headers_formset, headers_env_locked, model_maps),
        )

    def post(self, request, group_key):
        self.group = self._resolve_group(group_key)
        instance = SiteConfiguration.objects.get_instance()
        field_defaults = site_settings.get_defaults()
        cleared_secrets = {
            field_name
            for field_name in SiteConfiguration.ENCRYPTED_FIELDS
            if field_name in self.group.fields and request.POST.get(f"clear_{field_name}")
        }

        providers_formset, headers_formset, headers_env_locked = self._build_per_group_formsets(data=request.POST)
        providers_valid = providers_formset is None or providers_formset.is_valid()

        form = SiteConfigurationForm(
            request.POST,
            instance=instance,
            env_locked_fields=self._get_env_locked_fields(),
            cleared_secrets=cleared_secrets,
            field_defaults=field_defaults,
            group=self.group,
        )

        headers_valid = headers_formset is None or headers_env_locked or headers_formset.is_valid()
        model_maps = self._build_model_map_sections(data=request.POST, instance=instance)
        model_maps_valid = all(section.formset.is_valid() for section in model_maps if section.saves)

        if form.is_valid() and providers_valid and headers_valid and model_maps_valid:
            with transaction.atomic():
                if providers_formset is not None:
                    providers_formset.save()
                for section in model_maps:
                    if section.saves:
                        setattr(form.instance, section.field, model_map_from_formset(section.formset))
                form.save()
                if headers_formset is not None and not headers_env_locked:
                    headers_formset.save()
            messages.success(request, "Configuration saved.")
            for section in model_maps:
                for name in section.unknown_names() if section.saves else ():
                    messages.warning(request, section.unknown_warning % {"name": name})
            if providers_formset is not None:
                for warning in self._collect_provider_warnings(providers_formset):
                    messages.warning(request, warning)
            if self.group.key == "telegram":
                for warning in self._collect_telegram_warnings(form):
                    messages.warning(request, warning)
            return redirect("site_configuration", group_key=group_key)

        return render(
            request,
            self.template_name,
            self._build_context(form, providers_formset, headers_formset, headers_env_locked, model_maps),
        )

    @staticmethod
    def _resolve_group(group_key: str) -> FieldGroup:
        """Return the active ``FieldGroup`` or raise ``Http404`` for unknown keys."""
        try:
            return SiteConfiguration.get_group_by_key(group_key)
        except KeyError as exc:
            raise Http404(f"Unknown configuration group: {group_key!r}") from exc

    def _build_per_group_formsets(self, *, data) -> tuple[Any, Any, bool]:
        """Build the extra formsets the active group needs.

        Returns ``(providers_formset, headers_formset, headers_env_locked)``.
        Non-applicable formsets are ``None``.
        """
        if self.group.key == "providers":
            return self._build_providers_formset(data=data), None, False
        if self.group.key == "web_fetch":
            env_locked = site_settings.is_env_locked("web_fetch_auth_headers")
            return None, self._build_headers_formset(data=None if env_locked else data), env_locked
        return None, None, False

    def _build_model_map_sections(self, *, data, instance: SiteConfiguration) -> list[_ModelMapSection]:
        """The Agent group's model maps. A POST without a map's rows leaves that map as stored."""
        if self.group.key != "agent":
            return []
        from automation.agent.constants import EXPLORE_NAME
        from automation.agent.subagents import BUILTIN_SUBAGENT_NAMES

        maps: tuple[tuple[ModelMapKind, str, str, str, str, str, list[str]], ...] = (
            (
                "skills",
                "agent_skill_models",
                SKILL_MODELS_FORMSET_PREFIX,
                gettext("Skills"),
                gettext("Add skill"),
                gettext(
                    "No built-in or global skill is named '%(name)s'; its entry applies where a repository defines it."
                ),
                list(list_global_skills()),
            ),
            (
                "subagents",
                "agent_subagent_models",
                SUBAGENT_MODELS_FORMSET_PREFIX,
                gettext("Subagents"),
                gettext("Add subagent"),
                gettext("No built-in subagent is named '%(name)s'; its entry applies where a repository defines it."),
                sorted(BUILTIN_SUBAGENT_NAMES - {EXPLORE_NAME}),
            ),
        )
        sections = []
        for kind, field, prefix, title, add_label, unknown_warning, suggestions in maps:
            formset, env_value = None, None
            if site_settings.is_env_locked(field):
                env_value = getattr(site_settings, field)
            else:
                bound = data if data is not None and f"{prefix}-TOTAL_FORMS" in data else None
                initial = [{"name": name, "model": spec} for name, spec in getattr(instance, field).items()]
                formset = build_model_map_formset(kind, data=bound, initial=initial)
            sections.append(
                _ModelMapSection(kind, field, title, add_label, unknown_warning, suggestions, formset, env_value)
            )
        return sections

    def _build_context(
        self,
        form: SiteConfigurationForm,
        providers_formset,
        headers_formset,
        headers_env_locked: bool,
        model_maps: list[_ModelMapSection],
    ) -> dict:
        built_in_provider_forms, custom_provider_forms = self._split_provider_forms(providers_formset)
        return {
            "form": form,
            "active_group": self.group,
            "all_groups": SiteConfiguration.get_field_groups(),
            "providers_formset": providers_formset,
            "built_in_provider_forms": built_in_provider_forms,
            "custom_provider_forms": custom_provider_forms,
            "web_fetch_auth_headers_formset": headers_formset,
            "web_fetch_auth_headers_env_locked": headers_env_locked,
            "web_fetch_auth_headers_env_value": (site_settings.web_fetch_auth_headers if headers_env_locked else None),
            "telegram_bot_username": (site_settings.telegram_bot_username if self.group.key == "telegram" else None),
            "model_map_sections": model_maps,
        }

    @staticmethod
    def _split_provider_forms(formset) -> tuple[list, list]:
        if formset is None:
            return [], []
        built_in: list = []
        custom: list = []
        for form in formset.forms:
            if form.instance and form.instance.pk and form.instance.is_locked:
                built_in.append(form)
            else:
                custom.append(form)
        return built_in, custom

    @staticmethod
    def _build_headers_formset(*, data):
        return build_web_fetch_auth_header_formset()(
            data, queryset=WebFetchAuthHeader.objects.all(), prefix=WEB_FETCH_AUTH_HEADERS_FORMSET_PREFIX
        )

    @staticmethod
    def _build_providers_formset(*, data):
        return build_provider_formset()(data, queryset=Provider.objects.all(), prefix=PROVIDERS_FORMSET_PREFIX)

    @staticmethod
    def _collect_provider_warnings(formset) -> list[str]:
        out: list[str] = []
        for form in formset.forms:
            if not form.cleaned_data or form.cleaned_data.get("DELETE"):
                continue
            slug = form.cleaned_data.get("slug") or _("(new)")
            try:
                warnings = (form.base_url_version_warning, form.verify_ssl_warning)
            except Exception:
                # Warning collection must never 500 a successful save; the row is
                # already committed and the admin needs the success banner.
                logger.exception("Failed to collect provider warnings for %s", slug)
                continue
            out.extend(f"{slug}: {w}" for w in warnings if w)
        return out

    @staticmethod
    def _collect_telegram_warnings(form: SiteConfigurationForm) -> list[str]:
        """Run ``sync_telegram()`` now that the save has committed."""
        from notifications.telegram.config import sync_telegram

        warnings: list[str] = []
        if form.telegram_token_warning:
            warnings.append(form.telegram_token_warning)
        try:
            warnings.extend(sync_telegram())
        except Exception:
            # The row is already committed and the admin needs the success banner; a sync
            # failure must never 500 a successful save.
            logger.exception("Telegram sync failed after a configuration save")
            warnings.append(str(_("Telegram synchronisation failed; check the server logs.")))
        return warnings

    def _get_env_locked_fields(self) -> set[str]:
        """Return field names in the active group that are locked by an environment variable."""
        return {name for name in self.group.fields if site_settings.is_env_locked(name)}
