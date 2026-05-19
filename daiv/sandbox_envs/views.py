from __future__ import annotations

import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.functional import cached_property
from django.views import View
from django.views.generic import ListView

from accounts.mixins import AdminRequiredMixin
from sandbox_envs.forms import SandboxEnvironmentForm
from sandbox_envs.models import SandboxEnvironment
from sandbox_envs.services import build_env_trigger, humanise_global_default

logger = logging.getLogger("daiv.sandbox_envs")


def _is_htmx(request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _redirect_with_open(action: str, env_id=None):
    """Redirect to the list with ``?open=<action>[:<id>]`` so the list template's
    inline Alpine init can auto-open the drawer for deep links."""
    suffix = f"{action}:{env_id}" if env_id else action
    return redirect(f"{reverse('sandbox_envs:list')}?open={suffix}")


class EnvListView(LoginRequiredMixin, ListView):
    template_name = "sandbox_envs/list.html"
    context_object_name = "user_envs"

    def get_queryset(self):
        return SandboxEnvironment.objects.user_envs(self.request.user)

    def get_context_data(self, **kw):
        ctx = super().get_context_data(**kw)
        ctx["global_envs"] = SandboxEnvironment.objects.global_envs()
        ctx["is_admin"] = self.request.user.is_admin
        ctx["global_default_summary"] = humanise_global_default()
        return ctx


class EnvFormView(LoginRequiredMixin, View):
    """Drawer-only create / edit. Dispatches on the presence of ``pk`` in
    ``kwargs``. Non-HTMX GET redirects to the list with ``?open=`` so deep
    links open the drawer client-side."""

    http_method_names = ["get", "post"]

    def get_object(self):
        pk = self.kwargs.get("pk")
        if pk is None:
            return None
        return SandboxEnvironment.objects.scoped_get(self.request.user, pk)

    @staticmethod
    def _is_default_form(instance) -> bool:
        return bool(instance and instance.is_global_default)

    def _make_form(self, *, instance, data=None):
        return SandboxEnvironmentForm(
            data,
            instance=instance,
            user=self.request.user,
            is_admin=self.request.user.is_admin,
            is_default_form=self._is_default_form(instance),
        )

    @cached_property
    def _global_default_summary(self):
        return humanise_global_default()

    def _render(self, form, *, instance):
        return render(
            self.request,
            "sandbox_envs/_form_body.html",
            {
                "form": form,
                "object": instance,
                "show_delete": instance is not None,
                "is_default_form": self._is_default_form(instance),
                "global_default_summary": self._global_default_summary,
            },
        )

    def get(self, request, **_kw):
        instance = self.get_object()
        if not _is_htmx(request):
            action = "edit" if instance else "create"
            return _redirect_with_open(action, env_id=instance.id if instance else None)
        return self._render(self._make_form(instance=instance), instance=instance)

    def post(self, request, **_kw):
        instance = self.get_object()
        form = self._make_form(instance=instance, data=request.POST)
        if not form.is_valid():
            return self._render(form, instance=instance)
        try:
            env = form.save()
        except ValidationError as err:
            # full_clean() inside save() raises core.exceptions.ValidationError,
            # which is_valid() didn't catch; surface it as a non-field error.
            form.add_error(None, err)
            return self._render(form, instance=instance)
        action = "updated" if instance else "created"
        return HttpResponse(status=204, headers={"HX-Trigger": json.dumps(build_env_trigger(env, action))})


class EnvDeleteView(LoginRequiredMixin, View):
    http_method_names = ["get", "post"]

    def get_object(self):
        return SandboxEnvironment.objects.scoped_get(self.request.user, self.kwargs["pk"])

    def get(self, request, **_kw):
        env = self.get_object()
        if not _is_htmx(request):
            return _redirect_with_open("delete", env_id=env.id)
        return render(request, "sandbox_envs/_delete_body.html", {"object": env})

    def post(self, request, **_kw):
        env = self.get_object()
        ok, msg = env.can_delete()
        if not ok:
            return render(request, "sandbox_envs/_delete_body.html", {"object": env, "delete_error": msg})
        # Snapshot the trigger payload before the row goes away.
        payload = build_env_trigger(env, "deleted")
        env.delete()
        return HttpResponse(status=204, headers={"HX-Trigger": json.dumps(payload)})


class EnvSetDefaultView(AdminRequiredMixin, View):
    """On HTMX requests, swaps the ``#global-envs`` fragment in place; otherwise
    redirects to the list (covers no-JS / direct curl flows)."""

    http_method_names = ["post"]

    def post(self, request, pk):
        env = get_object_or_404(SandboxEnvironment.objects.global_envs(), pk=pk)
        try:
            env.promote_as_default()
        except ValidationError as err:
            # promote_as_default re-reads under FOR UPDATE; ValidationError here
            # means a concurrent admin deleted or rescoped the row mid-flight.
            logger.warning("set-default conflict for env_id=%s: %s", pk, err)
            return HttpResponse(err.messages[0] if err.messages else "Conflict", status=409)
        if _is_htmx(request):
            return render(
                request,
                "sandbox_envs/_global_envs.html",
                {"global_envs": SandboxEnvironment.objects.global_envs(), "is_admin": request.user.is_admin},
            )
        return redirect("sandbox_envs:list")
