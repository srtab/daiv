import logging
from typing import Any

from django.contrib import messages
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.html import format_html
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView

from skills.pages import SkillPage, SkillPageMixin
from skills.services import list_global_skills, skill_subagents

from automation.agent.constants import BUILTIN_SUBAGENT_NAMES, EXPLORE_MODEL_NAME, EXPLORE_NAME, GENERAL_PURPOSE_NAME
from configuration.forms import (
    PROVIDERS_FORMSET_PREFIX,
    RUN_MODEL_LABEL,
    WEB_FETCH_AUTH_HEADERS_FORMSET_PREFIX,
    ModelEntriesForm,
    ModelEntry,
    SiteConfigurationForm,
    build_provider_formset,
    build_web_fetch_auth_header_formset,
    model_spec_error,
)
from core.mixins import AdminRequiredMixin
from core.models import FieldGroup, Provider, SiteConfiguration, WebFetchAuthHeader
from core.site_settings import legacy_explore_model, site_settings
from core.utils import short_model_name

logger = logging.getLogger("daiv.configuration")


SUBAGENT_MODELS_PREFIX = "subagent_models"


def _explore_default_label():
    """What serves explore with no entry: the legacy variable's model, else its own, else the run's."""
    for spec in (legacy_explore_model(), EXPLORE_MODEL_NAME):
        if spec and not model_spec_error(spec):
            return short_model_name(spec)
    return RUN_MODEL_LABEL


def _unlisted_entries(instance: SiteConfiguration) -> list[ModelEntry]:
    """Stored entries no page lists, a repository's skill or subagent, so they can still be changed or cleared."""
    unlisted = (
        ("agent_subagent_models", lambda: BUILTIN_SUBAGENT_NAMES, _("A subagent only repositories define")),
        ("agent_skill_models", list_global_skills, _("A skill only repositories define")),
    )
    entries: list[ModelEntry] = []
    for site_field, listed, help_text in unlisted:
        stored = getattr(instance, site_field)
        if stored and not site_settings.is_env_locked(site_field):
            names = listed()
            entries += [ModelEntry(site_field, name, name, help_text) for name in stored if name not in names]
    return entries


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
        subagent_models = self._build_subagent_models_form(data=None, instance=instance)
        form = SiteConfigurationForm(
            instance=instance,
            env_locked_fields=self._get_env_locked_fields(),
            field_defaults=field_defaults,
            group=self.group,
        )
        return render(
            request,
            self.template_name,
            self._build_context(form, providers_formset, headers_formset, headers_env_locked, subagent_models),
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
        subagent_models = self._build_subagent_models_form(data=request.POST, instance=instance)
        saves_subagent_models = subagent_models is not None and subagent_models.is_submitted(request.POST)
        subagent_models_valid = not saves_subagent_models or subagent_models.is_valid()

        if form.is_valid() and providers_valid and headers_valid and subagent_models_valid:
            with transaction.atomic():
                if providers_formset is not None:
                    providers_formset.save()
                if saves_subagent_models:
                    subagent_models.save()
                form.save()
                if headers_formset is not None and not headers_env_locked:
                    headers_formset.save()
            messages.success(request, "Configuration saved.")
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
            self._build_context(form, providers_formset, headers_formset, headers_env_locked, subagent_models),
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

    def _build_subagent_models_form(self, *, data, instance: SiteConfiguration) -> ModelEntriesForm | None:
        """The Agent group's models of the subagents every run can delegate to, and the entries no skill page has."""
        if self.group.key != "agent":
            return None
        entries = [
            ModelEntry(
                "agent_subagent_models",
                GENERAL_PURPOSE_NAME,
                GENERAL_PURPOSE_NAME,
                _("Open-ended work the agent hands off"),
            ),
            ModelEntry(
                "agent_subagent_models",
                EXPLORE_NAME,
                EXPLORE_NAME,
                _("Read-only codebase search"),
                default_label=_explore_default_label(),
            ),
            *_unlisted_entries(instance),
        ]
        return ModelEntriesForm(entries, data, stored=instance, prefix=SUBAGENT_MODELS_PREFIX)

    def _build_context(
        self,
        form: SiteConfigurationForm,
        providers_formset,
        headers_formset,
        headers_env_locked: bool,
        subagent_models: ModelEntriesForm | None,
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
            "subagent_models_form": subagent_models,
            "subagent_models_env_var": site_settings.get_env_var_name("agent_subagent_models"),
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


def _mono(text: str) -> str:
    return format_html('<span class="font-mono text-[12px]">{}</span>', text)


def skill_model_entries(page: SkillPage) -> list[ModelEntry]:
    """The model entries of ``page``'s skill: its own, then one per subagent it ships."""
    declared = page.declared_model
    skill_help = format_html(
        gettext("Serves the run from the moment {} is invoked until the next message."), _mono(page.name)
    )
    if declared:
        declares = format_html(gettext("Its SKILL.md declares {}."), _mono(declared))
        skill_help = format_html("{} {}", skill_help, declares)
    skill_default = short_model_name(declared) or RUN_MODEL_LABEL
    return [
        ModelEntry("agent_skill_models", page.name, _("Skill"), skill_help, skill_default),
        *(
            ModelEntry(
                "agent_subagent_models",
                subagent.name,
                subagent.name,
                subagent.summary,
                short_model_name(subagent.model) or RUN_MODEL_LABEL,
            )
            for subagent in skill_subagents(page.name)
        ),
    ]


class SkillModelsView(AdminRequiredMixin, SkillPageMixin, TemplateView):
    """The skill page's Models tab: the site's model entries for the skill and the subagents it ships."""

    template_name = "configuration/skill_models.html"
    skill_tab = "models"

    def get(self, request, *args, **kwargs):
        return self.render_to_response(self.get_context_data(form=self._build_form(data=None)))

    def post(self, request, *args, **kwargs):
        form = self._build_form(data=request.POST)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        form.save()
        messages.success(request, _("Models saved."))
        return redirect("skills:models", name=self.skill_page.name)

    def _build_form(self, *, data) -> ModelEntriesForm:
        entries = skill_model_entries(self.skill_page)
        return ModelEntriesForm(entries, data, stored=SiteConfiguration.objects.get_instance())

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        form: ModelEntriesForm = context["form"]
        rows = [{"field": form[name], "stale": form.stale_reasons.get(name, "")} for name in form.fields]
        return {
            **context,
            "skill_row": rows[0],
            "subagent_rows": rows[1:],
            "skill_models_env_var": site_settings.get_env_var_name("agent_skill_models"),
            "subagent_models_env_var": site_settings.get_env_var_name("agent_subagent_models"),
        }
