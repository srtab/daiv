from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from sandbox_envs.forms import SandboxEnvironmentForm
from sandbox_envs.models import SandboxEnvironment, Scope


def _user_is_admin(user) -> bool:
    return bool(getattr(user, "is_admin", False)) or bool(getattr(user, "is_staff", False))


class EnvListView(LoginRequiredMixin, ListView):
    template_name = "sandbox_envs/list.html"
    context_object_name = "user_envs"

    def get_queryset(self):
        return SandboxEnvironment.objects.filter(scope=Scope.USER, user=self.request.user)

    def get_context_data(self, **kw):
        ctx = super().get_context_data(**kw)
        ctx["global_envs"] = SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).order_by("name")
        ctx["is_admin"] = _user_is_admin(self.request.user)
        return ctx


class _ScopedEnvMixin:
    """Restrict access: users see their own USER envs; admins can edit GLOBAL envs.

    Non-admin access to a GLOBAL env raises ``PermissionError`` (translated to 403
    by the calling view's ``dispatch``); non-owner access to a USER env raises
    ``Http404``.
    """

    def get_object(self, queryset=None):
        env_id = self.kwargs["pk"]
        env = get_object_or_404(SandboxEnvironment, pk=env_id)
        user = self.request.user
        is_admin = _user_is_admin(user)
        if env.scope == Scope.GLOBAL:
            if not is_admin:
                raise PermissionError("admin required")
            return env
        if env.user_id != user.id:
            raise Http404("Not found")
        return env


class EnvCreateView(LoginRequiredMixin, CreateView):
    template_name = "sandbox_envs/form.html"
    form_class = SandboxEnvironmentForm
    success_url = reverse_lazy("sandbox_envs:list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update({"user": self.request.user, "is_admin": _user_is_admin(self.request.user)})
        return kwargs


class EnvUpdateView(LoginRequiredMixin, _ScopedEnvMixin, UpdateView):
    template_name = "sandbox_envs/form.html"
    form_class = SandboxEnvironmentForm
    success_url = reverse_lazy("sandbox_envs:list")

    def dispatch(self, request, *args, **kwargs):
        try:
            return super().dispatch(request, *args, **kwargs)
        except PermissionError:
            return HttpResponseForbidden("Admin required for global environments")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update({
            "user": self.request.user,
            "is_admin": _user_is_admin(self.request.user),
            "is_default_form": (self.object.scope == Scope.GLOBAL and self.object.is_default),
        })
        return kwargs


class EnvDeleteView(LoginRequiredMixin, _ScopedEnvMixin, DeleteView):
    template_name = "sandbox_envs/delete_confirm.html"
    success_url = reverse_lazy("sandbox_envs:list")

    def dispatch(self, request, *args, **kwargs):
        try:
            return super().dispatch(request, *args, **kwargs)
        except PermissionError:
            return HttpResponseForbidden("Admin required for global environments")

    def form_valid(self, form):
        if self.object.is_default and self.object.scope == Scope.GLOBAL:
            return HttpResponse("Set another global environment as default before deleting this one.", status=409)
        return super().form_valid(form)


class EnvSetDefaultView(LoginRequiredMixin, View):
    """POST-only: mark a GLOBAL env as the new default. Admin-only."""

    def post(self, request, pk):
        if not _user_is_admin(request.user):
            return HttpResponseForbidden("Admin required")
        env = get_object_or_404(SandboxEnvironment, pk=pk, scope=Scope.GLOBAL)
        # Use the model's atomic helper (Task 3 dropped save() auto-demote).
        env.promote_as_default()
        return redirect("sandbox_envs:list")
