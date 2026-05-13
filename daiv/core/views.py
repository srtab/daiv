import logging

from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views import View

from accounts.mixins import AdminRequiredMixin
from core.forms import (
    PROVIDERS_FORMSET_PREFIX,
    WEB_FETCH_AUTH_HEADERS_FORMSET_PREFIX,
    SiteConfigurationForm,
    build_provider_formset,
    build_web_fetch_auth_header_formset,
)
from core.models import Provider, SiteConfiguration, WebFetchAuthHeader
from core.site_settings import site_settings

logger = logging.getLogger("daiv.core")


class HealthCheckView(View):
    """
    Simple health check endpoint that returns 200 OK.
    """

    async def get(self, request, *args, **kwargs):
        return HttpResponse("OK", content_type="text/plain")


class SiteConfigurationView(AdminRequiredMixin, View):
    """
    Admin-only view for managing site-wide configuration.
    """

    template_name = "core/site_configuration.html"

    def get(self, request):
        instance = SiteConfiguration.objects.get_instance()
        env_locked = self._get_env_locked_fields()
        field_defaults = site_settings.get_defaults()
        form = SiteConfigurationForm(instance=instance, env_locked_fields=env_locked, field_defaults=field_defaults)
        headers_env_locked = site_settings.is_env_locked("web_fetch_auth_headers")
        formset = self._build_headers_formset(data=None)
        providers_formset = self._build_providers_formset(data=None)
        return render(
            request, self.template_name, self._build_context(form, formset, providers_formset, headers_env_locked)
        )

    def post(self, request):
        instance = SiteConfiguration.objects.get_instance()
        env_locked = self._get_env_locked_fields()
        field_defaults = site_settings.get_defaults()

        cleared_secrets = {
            field_name for field_name in SiteConfiguration.ENCRYPTED_FIELDS if request.POST.get(f"clear_{field_name}")
        }

        # When env-locked, ignore submitted formset data and skip its save.
        headers_env_locked = site_settings.is_env_locked("web_fetch_auth_headers")
        formset = self._build_headers_formset(data=None if headers_env_locked else request.POST)
        providers_formset = self._build_providers_formset(data=request.POST)
        formset_valid = headers_env_locked or formset.is_valid()
        providers_valid = providers_formset.is_valid()
        # Always build the in-flight map (even when the providers formset is invalid)
        # so model-name validation reflects the user's *intent* rather than stale DB
        # state. The map only sees rows that passed field-level cleaning; invalid
        # rows are silently skipped, which is fine because save is gated on
        # ``providers_valid`` below.
        in_flight_providers = self._collect_in_flight_providers(providers_formset)

        form = SiteConfigurationForm(
            request.POST,
            instance=instance,
            env_locked_fields=env_locked,
            cleared_secrets=cleared_secrets,
            field_defaults=field_defaults,
            in_flight_providers=in_flight_providers,
        )

        if form.is_valid() and formset_valid and providers_valid:
            with transaction.atomic():
                # Providers save first so cache invalidation runs before downstream
                # readers within the same request (e.g. agent dispatch via a hook)
                # see the new state.
                providers_formset.save()
                form.save()
                if not headers_env_locked:
                    formset.save()
            messages.success(request, "Configuration saved.")
            return redirect("site_configuration")

        return render(
            request, self.template_name, self._build_context(form, formset, providers_formset, headers_env_locked)
        )

    @staticmethod
    def _build_headers_formset(*, data):
        return build_web_fetch_auth_header_formset()(
            data, queryset=WebFetchAuthHeader.objects.all(), prefix=WEB_FETCH_AUTH_HEADERS_FORMSET_PREFIX
        )

    @staticmethod
    def _build_providers_formset(*, data):
        return build_provider_formset()(data, queryset=Provider.objects.all(), prefix=PROVIDERS_FORMSET_PREFIX)

    @staticmethod
    def _collect_in_flight_providers(formset) -> dict[str, tuple[bool, bool]]:
        """Build a slug → (is_enabled, has_key) map from the providers formset.

        ``has_key`` precedence per row: ``clear_api_key`` forces ``False``;
        otherwise a newly submitted ``api_key`` forces ``True``; otherwise it
        falls back to whether the saved instance already has a stored key.
        Rows marked ``DELETE`` or missing a slug (e.g. JS-inserted but empty)
        are skipped.
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

    @staticmethod
    def _get_env_locked_fields() -> set[str]:
        """Collect field names locked by environment variables."""
        locked = set()
        for group in SiteConfiguration.get_field_groups():
            for field_name in group.fields:
                if site_settings.is_env_locked(field_name):
                    locked.add(field_name)
        return locked

    @staticmethod
    def _build_context(form: SiteConfigurationForm, formset, providers_formset, headers_env_locked: bool) -> dict:
        all_groups = SiteConfiguration.get_field_groups()
        groups = [g for g in all_groups if g.key == "providers" or any(f in form.fields for f in g.fields)]
        return {
            "form": form,
            "field_groups": groups,
            "web_fetch_auth_headers_formset": formset,
            "providers_formset": providers_formset,
            "web_fetch_auth_headers_env_locked": headers_env_locked,
            "web_fetch_auth_headers_env_value": site_settings.web_fetch_auth_headers if headers_env_locked else None,
        }
