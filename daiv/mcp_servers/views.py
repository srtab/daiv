from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView

from asgiref.sync import async_to_sync

from core.encryption import DecryptionError
from mcp_servers import services
from mcp_servers.forms import MCPServerForm, MCPServerHeaderFormSet, build_headers_from_formset, build_tool_choices
from mcp_servers.models import MCPServer

logger = logging.getLogger("daiv.mcp_servers")


def _decorate(servers, *, global_names=frozenset()):
    """Attach display-only health/exposed/filtered_out/shadowed attributes to each row.

    ``shadowed`` marks a personal server whose name collides with a global one:
    ``build_runtime_servers`` skips it at runtime (global wins), so the badge tells
    the owner it does not load — a condition the create-time guard can't catch when
    the global is added *after* the personal server."""
    for s in servers:
        s.health = services.server_health(s) if s.enabled else {"ok": True, "reason": None}
        s.exposed = services.exposed_tools(s)
        s.filtered_out = (
            len(s.discovered_tools) - len(s.exposed) if s.tool_filter_mode != MCPServer.FilterMode.NONE else 0
        )
        s.shadowed = s.is_shadowed_by(global_names)
    return servers


class _MCPFormKwargsMixin:
    """Shared form kwargs for the create/edit views: the acting user and their
    admin flag drive the form's scope choices and owner binding."""

    def _form_kwargs(self):
        return {"user": self.request.user, "is_admin": self.request.user.is_admin}


class MCPServerListView(LoginRequiredMixin, TemplateView):
    template_name = "mcp_servers/list.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        ctx["is_admin"] = user.is_admin
        # Fetch every global row once; derive the name set (for shadow detection) and
        # the custom/builtin split in Python instead of issuing a query for each.
        global_rows = list(MCPServer.objects.global_servers())
        global_names = {s.name for s in global_rows}
        ctx["your_servers"] = _decorate(list(MCPServer.objects.user_servers(user)), global_names=global_names)
        ctx["global_custom_servers"] = _decorate([s for s in global_rows if not s.is_builtin()])
        ctx["global_builtin_servers"] = _decorate([s for s in global_rows if s.is_builtin()])
        if user.is_admin:
            # Other members' personal servers — the admin's own are already in "Your servers".
            ctx["all_user_servers"] = _decorate(
                list(
                    MCPServer.objects
                    .filter(scope=MCPServer.Scope.USER)
                    .exclude(user=user)
                    .order_by("user__username", "name")
                ),
                global_names=global_names,
            )
        return ctx


class MCPServerCreateView(_MCPFormKwargsMixin, LoginRequiredMixin, View):
    http_method_names = ["get", "post"]
    template_name = "mcp_servers/form.html"

    def _formset_kwargs(self):
        # Members' new servers accept literal headers only.
        return {"form_kwargs": {"literal_only": not self.request.user.is_admin}}

    def get(self, request):
        return self._render(
            request,
            MCPServerForm(**self._form_kwargs()),
            MCPServerHeaderFormSet(prefix="headers", **self._formset_kwargs()),
        )

    def post(self, request):
        form = MCPServerForm(request.POST, **self._form_kwargs())
        formset = MCPServerHeaderFormSet(request.POST, prefix="headers", **self._formset_kwargs())
        if not (form.is_valid() and formset.is_valid()):
            return self._render(request, form, formset, status=400)
        obj = form.save(commit=False)
        obj.created_by = request.user
        # The owner (obj.user) is bound by MCPServerForm.save() for USER-scoped rows.
        obj.headers = build_headers_from_formset(formset, existing=None)
        obj.save()
        result = services.sync_discovered_tools(obj)
        messages.success(request, _("MCP server '%(name)s' created.") % {"name": obj.name})
        if not result.get("ok"):
            messages.warning(
                request,
                _("'%(name)s' was saved, but its tools couldn't be refreshed: %(error)s")
                % {"name": obj.name, "error": result.get("error") or _("unknown error")},
            )
        return redirect(reverse("mcp_servers:list"))

    def _render(self, request, form, formset, *, status=200):
        # Create never has a saved server to discover against, so there are no
        # checkbox choices — the template renders the textarea fallback.
        return render(
            request,
            self.template_name,
            {"form": form, "formset": formset, "mode": "create", "tool_choices": []},
            status=status,
        )


class MCPServerEditView(_MCPFormKwargsMixin, LoginRequiredMixin, View):
    http_method_names = ["get", "post"]
    template_name = "mcp_servers/form.html"

    def _formset_kwargs(self, obj):
        return {"form_kwargs": {"literal_only": obj.is_user_scoped}}

    def get(self, request, pk):
        obj = MCPServer.objects.scoped_get(request.user, pk)
        initial, headers_locked = _existing_headers_for_formset(obj)
        if headers_locked:
            messages.error(
                request,
                _(
                    "Stored headers for '%(name)s' could not be decrypted (encryption key changed?). "
                    "Saving from here is disabled until you reset them manually."
                )
                % {"name": obj.name},
            )
        form = MCPServerForm(instance=obj, **self._form_kwargs())
        formset = MCPServerHeaderFormSet(initial=initial, prefix="headers", **self._formset_kwargs(obj))
        return self._render(request, obj, form, formset, selected=obj.tool_filter_items, headers_locked=headers_locked)

    def post(self, request, pk):
        obj = MCPServer.objects.scoped_get(request.user, pk)
        try:
            existing_headers = obj.headers or []
        except DecryptionError:
            # build_headers_from_formset would otherwise overwrite the unreadable ciphertext with [].
            messages.error(
                request,
                _(
                    "Cannot save '%(name)s': existing headers could not be decrypted. "
                    "Delete the server and re-create it, or rotate the encryption key back."
                )
                % {"name": obj.name},
            )
            return redirect(reverse("mcp_servers:edit", args=[obj.pk]))

        form = MCPServerForm(request.POST, instance=obj, **self._form_kwargs())
        formset = MCPServerHeaderFormSet(request.POST, prefix="headers", **self._formset_kwargs(obj))
        if not (form.is_valid() and formset.is_valid()):
            return self._render(
                request, obj, form, formset, selected=request.POST.getlist("tool_filter_items"), status=400
            )
        saved = form.save(commit=False)
        saved.headers = build_headers_from_formset(formset, existing=existing_headers)
        saved.save()
        result = services.sync_discovered_tools(saved)
        messages.success(request, _("MCP server '%(name)s' updated.") % {"name": obj.name})
        if not result.get("ok"):
            messages.warning(
                request,
                _("'%(name)s' was saved, but its tools couldn't be refreshed: %(error)s")
                % {"name": obj.name, "error": result.get("error") or _("unknown error")},
            )
        return redirect(reverse("mcp_servers:list"))

    def _render(self, request, obj, form, formset, *, selected, headers_locked=False, status=200):
        # Choices come from the persisted snapshot — no network on render.
        discovered = obj.discovered_tools or []
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "formset": formset,
                "mode": "edit",
                "object": obj,
                "builtin": obj.is_builtin(),
                "headers_locked": headers_locked,
                "tool_choices": build_tool_choices(discovered, selected),
            },
            status=status,
        )


class MCPServerDeleteView(LoginRequiredMixin, View):
    http_method_names = ["get", "post"]

    def _reject_builtin(self, request, obj):
        """Built-ins cannot be deleted (the model's ``delete()`` would raise);
        flash + redirect instead. Returns a response to short-circuit, else None."""
        if obj.is_builtin():
            messages.error(request, _("Built-in MCP servers cannot be deleted."))
            return redirect(reverse("mcp_servers:list"))
        return None

    def get(self, request, pk):
        obj = MCPServer.objects.manageable_get(request.user, pk)
        if reject := self._reject_builtin(request, obj):
            return reject
        return render(request, "mcp_servers/confirm_delete.html", {"object": obj})

    def post(self, request, pk):
        obj = MCPServer.objects.manageable_get(request.user, pk)
        if reject := self._reject_builtin(request, obj):
            return reject
        name = obj.name
        obj.delete()
        messages.success(request, _("MCP server '%(name)s' deleted.") % {"name": name})
        return redirect(reverse("mcp_servers:list"))


class MCPServerToggleView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, pk):
        obj = MCPServer.objects.manageable_get(request.user, pk)
        obj.enabled = not obj.enabled
        obj.save(update_fields=["enabled", "modified"])
        return redirect(reverse("mcp_servers:list"))


class MCPServerRefreshToolsView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, pk):
        obj = MCPServer.objects.manageable_get(request.user, pk)
        result = services.sync_discovered_tools(obj)
        if result.get("ok"):
            messages.success(
                request,
                _("Refreshed tools for '%(name)s' (%(count)d found).") % {"name": obj.name, "count": result["count"]},
            )
        else:
            messages.error(
                request,
                _("Could not refresh tools for '%(name)s': %(error)s")
                % {"name": obj.name, "error": result.get("error") or _("unknown error")},
            )
        if request.POST.get("next") == "edit":
            return redirect(reverse("mcp_servers:edit", args=[obj.pk]))
        return redirect(reverse("mcp_servers:list"))


class MCPServerTestView(LoginRequiredMixin, View):
    """Probe an MCP endpoint from the form's "Test connection" button.

    Login-required. The stored-secret borrow (matched by the ``name`` POST param
    against a saved row) is SCOPED: a member resolves secrets only from their own
    ``scope=user`` rows; an admin from a GLOBAL row or their OWN personal row —
    never from another member's personal server (see ``_resolve_borrowable``). A
    ``mode=env_ref`` header in the probe payload is rejected for non-admins so an
    unsaved probe cannot read host env vars either."""

    http_method_names = ["post"]

    def post(self, request):
        formset = MCPServerHeaderFormSet(
            request.POST, prefix="headers", form_kwargs={"literal_only": not request.user.is_admin}
        )
        if not formset.is_valid():
            return JsonResponse({"ok": False, "error": "invalid headers"}, status=400)
        # Re-testing a saved server: the form blanks preserved literal values (see
        # _existing_headers_for_formset), so resolve them from the stored row here —
        # otherwise a blank "preserve existing" secret would probe without it and fail.
        existing_headers = None
        name = request.POST.get("name")
        if name:
            obj = self._resolve_borrowable(request.user, name)
            if obj is not None:
                try:
                    existing_headers = obj.headers or []
                except DecryptionError:
                    # Refuse rather than probe without the secret: proceeding would
                    # send an unauthenticated request and misattribute the resulting
                    # failure to the remote server instead of the local key problem.
                    logger.warning(
                        "MCP test-connection: cannot borrow headers for '%s' (pk=%s); decryption failed",
                        obj.name,
                        obj.pk,
                    )
                    return JsonResponse(
                        {"ok": False, "error": "stored headers could not be decrypted; reset them before testing"},
                        status=400,
                    )
        headers = build_headers_from_formset(formset, existing=existing_headers)
        if not request.user.is_admin and any(h.get("mode") == MCPServer.HeaderMode.ENV_REF for h in headers):
            return JsonResponse({"ok": False, "error": "env_ref headers are not allowed"}, status=400)
        payload = {"transport": request.POST.get("transport"), "url": request.POST.get("url"), "headers": headers}
        result = async_to_sync(services.test_connection)(payload)
        return JsonResponse(result, status=200 if result.get("ok") else 502)

    @staticmethod
    def _resolve_borrowable(user, name):
        """The saved row whose stored secret this user may borrow when probing.
        Admins: a GLOBAL row named ``name`` (which wins the ambiguity), else their
        OWN user-scoped row. Members: only their own user-scoped row named ``name``.
        Neither may borrow another member's personal secret — that stays private to
        its owner, matching the per-user isolation the runtime enforces (an admin's
        own run never loads another member's rows)."""
        own = MCPServer.objects.filter(name=name, scope=MCPServer.Scope.USER, user=user)
        if user.is_admin:
            return MCPServer.objects.filter(name=name, scope=MCPServer.Scope.GLOBAL).first() or own.first()
        return own.first()


def _existing_headers_for_formset(obj: MCPServer) -> tuple[list[dict], bool]:
    """Return ``(initial, headers_locked)`` for the headers formset.

    Literal values are blanked: ``build_headers_from_formset`` treats blank
    on POST as "preserve existing". Each row carries a ``value_stored`` flag so
    ``MCPServerHeaderForm`` can advertise "a value is stored here" on the
    otherwise-empty input. ``headers_locked`` is True when the ciphertext can't
    be decoded — callers must refuse the POST so the recoverable ciphertext
    isn't overwritten with [].
    """
    try:
        rows = obj.headers or []
    except DecryptionError:
        logger.warning("Headers for MCP server %r could not be decrypted.", obj.name)
        return [], True
    out: list[dict] = []
    for h in rows:
        value = h.get("value", "")
        blanked = h.get("mode") == MCPServer.HeaderMode.LITERAL and bool(value)
        if blanked:
            value = ""
        out.append({
            "name": h.get("name", ""),
            "mode": h.get("mode", MCPServer.HeaderMode.LITERAL.value),
            "value": value,
            "value_stored": blanked,
        })
    return out, False
