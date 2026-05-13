import logging

from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views import View

from accounts.mixins import AdminRequiredMixin
from core.forms import WEB_FETCH_AUTH_HEADERS_FORMSET_PREFIX, SiteConfigurationForm, build_web_fetch_auth_header_formset
from core.models import SiteConfiguration, WebFetchAuthHeader
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
        formset = self._build_formset(data=None)
        return render(request, self.template_name, self._build_context(form, formset, headers_env_locked))

    def post(self, request):
        instance = SiteConfiguration.objects.get_instance()
        env_locked = self._get_env_locked_fields()
        field_defaults = site_settings.get_defaults()

        cleared_secrets = {
            field_name for field_name in SiteConfiguration.ENCRYPTED_FIELDS if request.POST.get(f"clear_{field_name}")
        }

        form = SiteConfigurationForm(
            request.POST,
            instance=instance,
            env_locked_fields=env_locked,
            cleared_secrets=cleared_secrets,
            field_defaults=field_defaults,
        )

        # When env-locked, ignore submitted formset data and skip its save.
        headers_env_locked = site_settings.is_env_locked("web_fetch_auth_headers")
        formset = self._build_formset(data=None if headers_env_locked else request.POST)
        formset_valid = headers_env_locked or formset.is_valid()

        if form.is_valid() and formset_valid:
            with transaction.atomic():
                form.save()
                if not headers_env_locked:
                    formset.save()
            messages.success(request, "Configuration saved.")
            return redirect("site_configuration")

        return render(request, self.template_name, self._build_context(form, formset, headers_env_locked))

    @staticmethod
    def _build_formset(*, data):
        return build_web_fetch_auth_header_formset()(
            data, queryset=WebFetchAuthHeader.objects.all(), prefix=WEB_FETCH_AUTH_HEADERS_FORMSET_PREFIX
        )

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
    def _build_context(form: SiteConfigurationForm, formset, headers_env_locked: bool) -> dict:
        groups = [g for g in SiteConfiguration.get_field_groups() if any(f in form.fields for f in g.fields)]
        return {
            "form": form,
            "field_groups": groups,
            "web_fetch_auth_headers_formset": formset,
            "web_fetch_auth_headers_env_locked": headers_env_locked,
            "web_fetch_auth_headers_env_value": site_settings.web_fetch_auth_headers if headers_env_locked else None,
        }
