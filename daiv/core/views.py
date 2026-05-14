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


class SiteConfigurationIndexView(AdminRequiredMixin, View):
    """Redirect admins to the first configuration group (agent)."""

    def get(self, request):
        return redirect("site_configuration", group_key="agent")


class SiteConfigurationGroupView(AdminRequiredMixin, View):
    """
    Admin-only view for managing one ``FieldGroup`` of the site-wide configuration.

    The active group is determined by the URL kwarg ``group_key``. Unknown keys
    raise ``Http404``. Saves are scoped to the group's fields only.
    """

    template_name = "core/site_configuration_group.html"

    def dispatch(self, request, *args, **kwargs):
        self.group = self._resolve_group(kwargs["group_key"])
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, group_key):
        instance = SiteConfiguration.objects.get_instance()
        env_locked = self._get_env_locked_fields()
        field_defaults = site_settings.get_defaults()
        form = SiteConfigurationForm(
            instance=instance, env_locked_fields=env_locked, field_defaults=field_defaults, group=self.group
        )
        return render(request, self.template_name, self._build_context(form, data=None))

    def post(self, request, group_key):
        instance = SiteConfiguration.objects.get_instance()
        env_locked = self._get_env_locked_fields()
        field_defaults = site_settings.get_defaults()

        cleared_secrets = {
            field_name
            for field_name in SiteConfiguration.ENCRYPTED_FIELDS
            if field_name in self.group.fields and request.POST.get(f"clear_{field_name}")
        }

        providers_formset = None
        headers_formset = None
        headers_env_locked = False
        in_flight_providers: dict[str, tuple[bool, bool]] | None = None

        if self.group.key == "providers":
            providers_formset = self._build_providers_formset(data=request.POST)
            # Validate first so cleaned_data is available for in-flight map construction.
            # (cleaned_data only exists after is_valid(); invalid rows are silently skipped
            # by _collect_in_flight_providers, so we can call it even when formset is invalid.)
            providers_valid = providers_formset.is_valid()
            in_flight_providers = self._collect_in_flight_providers(providers_formset)
        else:
            providers_valid = True
            if self.group.key == "web_fetch":
                headers_env_locked = site_settings.is_env_locked("web_fetch_auth_headers")
                headers_formset = self._build_headers_formset(data=None if headers_env_locked else request.POST)

        form = SiteConfigurationForm(
            request.POST,
            instance=instance,
            env_locked_fields=env_locked,
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
            return redirect("site_configuration", group_key=group_key)

        return render(
            request,
            self.template_name,
            self._build_context(
                form,
                providers_formset=providers_formset,
                headers_formset=headers_formset,
                headers_env_locked=headers_env_locked,
                data=request.POST,
            ),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @classmethod
    def _resolve_group(cls, group_key: str):
        for group in SiteConfiguration.get_field_groups():
            if group.key == group_key:
                return group
        from django.http import Http404

        raise Http404(f"Unknown configuration group: {group_key!r}")

    def _build_context(
        self,
        form: SiteConfigurationForm,
        *,
        providers_formset=None,
        headers_formset=None,
        headers_env_locked: bool = False,
        data=None,
    ) -> dict:
        # Build the per-page formset(s) for GET (data is None) when relevant.
        if self.group.key == "providers" and providers_formset is None:
            providers_formset = self._build_providers_formset(data=None)
        if self.group.key == "web_fetch" and headers_formset is None:
            headers_env_locked = site_settings.is_env_locked("web_fetch_auth_headers")
            headers_formset = self._build_headers_formset(data=None)

        all_groups = SiteConfiguration.get_field_groups()
        return {
            "form": form,
            "active_group": self.group,
            "all_groups": all_groups,
            "providers_formset": providers_formset,
            "web_fetch_auth_headers_formset": headers_formset,
            "web_fetch_auth_headers_env_locked": headers_env_locked,
            "web_fetch_auth_headers_env_value": (site_settings.web_fetch_auth_headers if headers_env_locked else None),
        }

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

        Identical to the original helper from ``SiteConfigurationView``: rows
        marked ``DELETE`` or missing a slug are skipped; ``clear_api_key`` forces
        ``has_key=False``; otherwise a newly submitted ``api_key`` forces
        ``True``; otherwise falls back to the saved instance.
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
        """Env-locked fields among *this group's* fields only.

        Limiting the scan to the active group avoids touching unrelated
        env vars on every request.
        """
        return {name for name in self.group.fields if site_settings.is_env_locked(name)}
