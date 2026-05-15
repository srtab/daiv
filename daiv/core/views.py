import logging
from typing import Any

from django.contrib import messages
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext_lazy as _
from django.views import View

from accounts.mixins import AdminRequiredMixin
from core.forms import (
    PROVIDERS_FORMSET_PREFIX,
    WEB_FETCH_AUTH_HEADERS_FORMSET_PREFIX,
    SiteConfigurationForm,
    build_provider_formset,
    build_web_fetch_auth_header_formset,
)
from core.models import FieldGroup, Provider, SiteConfiguration, WebFetchAuthHeader
from core.site_settings import site_settings

logger = logging.getLogger("daiv.core")


class HealthCheckView(View):
    """
    Simple health check endpoint that returns 200 OK.
    """

    async def get(self, request, *args, **kwargs):
        return HttpResponse("OK", content_type="text/plain")


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

    template_name = "core/site_configuration_group.html"

    # Resolved by the GET/POST handlers from the URL kwarg ``group_key``. Declared at
    # class level so the type is visible to readers and tools without inferring it
    # from the assignment site.
    group: FieldGroup

    def get(self, request, group_key):
        self.group = self._resolve_group(group_key)
        instance = SiteConfiguration.objects.get_instance()
        field_defaults = site_settings.get_defaults()
        providers_formset, headers_formset, headers_env_locked = self._build_per_group_formsets(data=None)
        form = SiteConfigurationForm(
            instance=instance,
            env_locked_fields=self._get_env_locked_fields(),
            field_defaults=field_defaults,
            group=self.group,
        )
        return render(
            request,
            self.template_name,
            self._build_context(form, providers_formset, headers_formset, headers_env_locked),
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

        # cleaned_data is only populated after is_valid(); invalid rows are silently skipped
        # by _collect_in_flight_providers, so calling it even when the formset is invalid is fine.
        providers_valid = providers_formset is None or providers_formset.is_valid()
        in_flight_providers = (
            self._collect_in_flight_providers(providers_formset) if providers_formset is not None else None
        )

        form = SiteConfigurationForm(
            request.POST,
            instance=instance,
            env_locked_fields=self._get_env_locked_fields(),
            cleared_secrets=cleared_secrets,
            field_defaults=field_defaults,
            in_flight_providers=in_flight_providers,
            group=self.group,
        )

        headers_valid = headers_formset is None or headers_env_locked or headers_formset.is_valid()

        if form.is_valid() and providers_valid and headers_valid:
            with transaction.atomic():
                if providers_formset is not None:
                    providers_formset.save()
                form.save()
                if headers_formset is not None and not headers_env_locked:
                    headers_formset.save()
            messages.success(request, "Configuration saved.")
            if providers_formset is not None:
                for warning in self._collect_provider_warnings(providers_formset):
                    messages.warning(request, warning)
            return redirect("site_configuration", group_key=group_key)

        return render(
            request,
            self.template_name,
            self._build_context(form, providers_formset, headers_formset, headers_env_locked),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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

    def _build_context(
        self, form: SiteConfigurationForm, providers_formset, headers_formset, headers_env_locked: bool
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
    def _collect_in_flight_providers(formset) -> dict[str, tuple[bool, bool]]:
        """Build a slug → (is_enabled, has_key) map from the providers formset.

        Rows marked ``DELETE`` or missing a slug are skipped. For ``has_key``:
        ``clear_api_key`` forces ``False``; a newly submitted ``api_key`` forces
        ``True``; otherwise falls back to whether the saved instance has a stored key.
        """
        state: dict[str, tuple[bool, bool]] = {}
        for form in formset.forms:
            cleaned = form.cleaned_data
            if not cleaned or cleaned.get("DELETE"):
                continue
            slug = cleaned.get("slug")
            if not slug:
                continue
            is_enabled = bool(cleaned.get("is_enabled"))
            if cleaned.get("clear_api_key"):
                has_key = False
            elif cleaned.get("api_key"):
                has_key = True
            else:
                has_key = bool(form.instance and form.instance.pk and form.instance.api_key)
            state[slug] = (is_enabled, has_key)
        return state

    def _get_env_locked_fields(self) -> set[str]:
        """Return field names in the active group that are locked by an environment variable."""
        return {name for name in self.group.fields if site_settings.is_env_locked(name)}
