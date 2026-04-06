import logging

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views import View

from accounts.mixins import AdminRequiredMixin
from automation.agent.constants import ModelName
from core.forms import SiteConfigurationForm
from core.models import SiteConfiguration
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
        return render(request, self.template_name, self._build_context(form))

    def post(self, request):
        instance = SiteConfiguration.objects.get_instance()
        env_locked = self._get_env_locked_fields()
        field_defaults = site_settings.get_defaults()

        cleared_secrets = set()
        for field_name in SiteConfiguration.ENCRYPTED_FIELDS:
            if request.POST.get(f"clear_{field_name}"):
                cleared_secrets.add(field_name)

        form = SiteConfigurationForm(
            request.POST,
            instance=instance,
            env_locked_fields=env_locked,
            cleared_secrets=cleared_secrets,
            field_defaults=field_defaults,
        )
        if form.is_valid():
            form.save()
            messages.success(request, "Configuration saved.")
            return redirect("site_configuration")

        return render(request, self.template_name, self._build_context(form))

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
    def _build_context(form: SiteConfigurationForm) -> dict:
        return {
            "form": form,
            "field_groups": SiteConfiguration.get_field_groups(),
            "model_name_choices": [(m.value, m.value) for m in ModelName],
        }
