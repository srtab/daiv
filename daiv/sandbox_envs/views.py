from __future__ import annotations

import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse, HttpResponseForbidden, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from accounts.mixins import AdminRequiredMixin
from sandbox_envs.forms import SandboxEnvironmentForm
from sandbox_envs.models import SandboxEnvironment, Scope
from sandbox_envs.services import humanise_global_default

logger = logging.getLogger("daiv.sandbox_envs")


def _encode_env_vars_for_template(env: SandboxEnvironment) -> str:
    """Serialise an env's env_vars list for the template's JS editor.

    Secret values are masked (rendered as an empty string) so decrypted secrets
    never reach the page HTML. ``has_existing_value`` is a UI hint so the editor
    can show a "keep existing value" affordance for stored secrets.

    Returns an empty list when decryption fails — the user can re-enter values
    via the form, and the form's :meth:`_preserve_unchanged_secrets` will block
    save with a clear error if the underlying ciphertext is still unreadable.
    """
    from core.encryption import DecryptionError

    try:
        rows = env.env_vars or []
    except DecryptionError:
        logger.error("env_vars decryption failed for SandboxEnvironment id=%s; rendering empty editor", env.id)
        rows = []
    masked = [
        {
            "name": r.get("name", ""),
            "value": "" if r.get("is_secret") else r.get("value", ""),
            "is_secret": bool(r.get("is_secret")),
            "has_existing_value": bool(r.get("is_secret")),
        }
        for r in rows
    ]
    return json.dumps(masked)


def _encode_repo_ids_for_template(env: SandboxEnvironment) -> str:
    return json.dumps(list(env.repo_ids or []))


def _is_htmx(request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _global_envs_context(user) -> dict:
    return {
        "global_envs": SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).order_by("name"),
        "is_admin": user.is_admin,
    }


def _global_default_summary_context() -> dict:
    return {"global_default_summary": humanise_global_default()}


class EnvListView(LoginRequiredMixin, ListView):
    template_name = "sandbox_envs/list.html"
    context_object_name = "user_envs"

    def get_queryset(self):
        return SandboxEnvironment.objects.filter(scope=Scope.USER, user=self.request.user)

    def get_context_data(self, **kw):
        ctx = super().get_context_data(**kw)
        ctx.update(_global_envs_context(self.request.user))
        return ctx


class _ScopedEnvMixin:
    """Object-scoping for `Env(Update|Delete)View`.

    Raises ``PermissionError`` (not ``PermissionDenied``) for GLOBAL-as-non-admin
    so the calling view's ``dispatch`` can translate it to a 403 with a custom
    message; the standard ``PermissionDenied`` would render Django's generic
    403 page instead. Non-owner USER access raises ``Http404``.
    """

    def get_object(self, queryset=None):
        env_id = self.kwargs["pk"]
        env = get_object_or_404(SandboxEnvironment, pk=env_id)
        user = self.request.user
        if env.scope == Scope.GLOBAL:
            if not user.is_admin:
                raise PermissionError("admin required")
            return env
        if env.user_id != user.id:
            raise Http404("Not found")
        return env


class EnvCreateView(LoginRequiredMixin, CreateView):
    """HX-Request flips this into a drawer-friendly mode:

    GET returns just ``_form_body.html`` (no page chrome); POST on success returns
    204 + an ``HX-Trigger: env-created`` event carrying the new env's id/scope/name
    so the host page can append the option and select it without a reload.
    Validation failures re-render the form body so HTMX can swap it back in place.
    """

    template_name = "sandbox_envs/form.html"
    form_class = SandboxEnvironmentForm
    success_url = reverse_lazy("sandbox_envs:list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update({"user": self.request.user, "is_admin": self.request.user.is_admin})
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        form = ctx["form"]
        submitted = form.data.get("env_vars_json") if form.is_bound else None
        ctx["env_vars_initial"] = submitted or "[]"
        submitted_repos = form.data.get("repo_ids_json") if form.is_bound else None
        ctx["repo_ids_initial"] = submitted_repos or "[]"
        ctx["in_drawer"] = _is_htmx(self.request)
        ctx["is_default_form"] = False
        ctx.update(_global_default_summary_context())
        return ctx

    def get_template_names(self):
        return ["sandbox_envs/_form_body.html"] if _is_htmx(self.request) else [self.template_name]

    def form_valid(self, form):
        try:
            env = form.save()
        except ValidationError as err:
            # ``form.save()`` runs ``instance.full_clean()`` which raises
            # ``django.core.exceptions.ValidationError`` (not the forms one), so
            # it never reaches ``form_invalid`` on its own.
            form.add_error(None, err)
            return self.form_invalid(form)
        if not _is_htmx(self.request):
            return HttpResponseRedirect(self.get_success_url())
        payload = {
            "env-created": {
                "id": str(env.id),
                "name": env.name,
                "scope": env.scope,
                "scope_display": env.get_scope_display(),
                "is_default": env.is_default,
                "summary": env.summary,
            }
        }
        return HttpResponse(status=204, headers={"HX-Trigger": json.dumps(payload)})


class EnvUpdateView(LoginRequiredMixin, _ScopedEnvMixin, UpdateView):
    """HX-Request flips this into a drawer-friendly mode, mirroring `EnvCreateView`."""

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
            "is_admin": self.request.user.is_admin,
            "is_default_form": (self.object.scope == Scope.GLOBAL and self.object.is_default),
        })
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        form = ctx["form"]
        submitted = form.data.get("env_vars_json") if form.is_bound else None
        ctx["env_vars_initial"] = submitted or _encode_env_vars_for_template(self.object)
        submitted_repos = form.data.get("repo_ids_json") if form.is_bound else None
        ctx["repo_ids_initial"] = submitted_repos or _encode_repo_ids_for_template(self.object)
        ctx["show_delete"] = True
        ctx["in_drawer"] = _is_htmx(self.request)
        ctx["is_default_form"] = self.object.scope == Scope.GLOBAL and self.object.is_default
        ctx.update(_global_default_summary_context())
        return ctx

    def get_template_names(self):
        return ["sandbox_envs/_form_body.html"] if _is_htmx(self.request) else [self.template_name]

    def form_valid(self, form):
        try:
            env = form.save()
        except ValidationError as err:
            form.add_error(None, err)
            return self.form_invalid(form)
        if not _is_htmx(self.request):
            return HttpResponseRedirect(self.get_success_url())
        payload = {
            "env-updated": {
                "id": str(env.id),
                "name": env.name,
                "scope": env.scope,
                "scope_display": env.get_scope_display(),
                "is_default": env.is_default,
                "summary": env.summary,
            }
        }
        return HttpResponse(status=204, headers={"HX-Trigger": json.dumps(payload)})


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


class EnvSetDefaultView(AdminRequiredMixin, View):
    """On HTMX requests, swaps the ``#global-envs`` fragment in place; otherwise
    redirects to the list (covers no-JS / direct curl flows)."""

    http_method_names = ["post"]

    def post(self, request, pk):
        env = get_object_or_404(SandboxEnvironment, pk=pk, scope=Scope.GLOBAL)
        try:
            env.promote_as_default()
        except ValidationError as err:
            # ``promote_as_default`` re-reads under FOR UPDATE; ValidationError here
            # means a concurrent admin deleted or rescoped the row mid-flight.
            logger.warning("set-default conflict for env_id=%s: %s", pk, err)
            return HttpResponse(err.messages[0] if err.messages else "Conflict", status=409)
        if _is_htmx(request):
            html = render_to_string(
                "sandbox_envs/_global_envs.html", _global_envs_context(request.user), request=request
            )
            return HttpResponse(html, content_type="text/html")
        return redirect("sandbox_envs:list")
