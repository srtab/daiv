from __future__ import annotations

import logging

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView

from asgiref.sync import async_to_sync

from accounts.mixins import AdminRequiredMixin
from core.encryption import DecryptionError
from mcp_servers import services
from mcp_servers.forms import MCPServerForm, MCPServerHeaderFormSet, build_headers_from_formset
from mcp_servers.models import MCPServer

logger = logging.getLogger("daiv.mcp_servers")


class MCPServerListView(AdminRequiredMixin, TemplateView):
    template_name = "mcp_servers/list.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        custom = list(MCPServer.objects.filter(source=MCPServer.Source.CUSTOM).select_related("created_by"))
        for s in custom:
            s.health = services.server_health(s) if s.enabled else {"ok": True, "reason": None}
        ctx["custom_servers"] = custom
        ctx["builtin_servers"] = list(MCPServer.objects.filter(source=MCPServer.Source.BUILTIN))
        return ctx


class MCPServerCreateView(AdminRequiredMixin, View):
    http_method_names = ["get", "post"]
    template_name = "mcp_servers/form.html"

    def get(self, request):
        return self._render(request, MCPServerForm(), MCPServerHeaderFormSet(prefix="headers"))

    def post(self, request):
        form = MCPServerForm(request.POST)
        formset = MCPServerHeaderFormSet(request.POST, prefix="headers")
        if not (form.is_valid() and formset.is_valid()):
            return self._render(request, form, formset, status=400)
        obj = form.save(commit=False)
        obj.created_by = request.user
        obj.headers = build_headers_from_formset(formset, existing=None)
        obj.save()
        messages.success(request, _("MCP server '%(name)s' created.") % {"name": obj.name})
        return redirect(reverse("mcp_servers:list"))

    def _render(self, request, form, formset, *, status=200):
        return render(request, self.template_name, {"form": form, "formset": formset, "mode": "create"}, status=status)


class MCPServerEditView(AdminRequiredMixin, View):
    http_method_names = ["get", "post"]
    template_name = "mcp_servers/form.html"

    def get(self, request, name):
        obj = get_object_or_404(MCPServer, name=name)
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
        discovered = services.discover_tools_cached(obj)
        form = MCPServerForm(instance=obj, discovered_tools=discovered or None)
        formset = MCPServerHeaderFormSet(initial=initial, prefix="headers")
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
            },
        )

    def post(self, request, name):
        obj = get_object_or_404(MCPServer, name=name)
        if obj.is_builtin():
            # Only ``enabled`` may change. Apply it directly and ignore everything else.
            obj.enabled = request.POST.get("enabled") == "on"
            obj.save(update_fields=["enabled", "modified"])
            messages.success(request, _("MCP server '%(name)s' updated.") % {"name": obj.name})
            return redirect(reverse("mcp_servers:list"))

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
            return redirect(reverse("mcp_servers:edit", args=[obj.name]))

        discovered = services.discover_tools_cached(obj)
        form = MCPServerForm(request.POST, instance=obj, discovered_tools=discovered or None)
        formset = MCPServerHeaderFormSet(request.POST, prefix="headers")
        if not (form.is_valid() and formset.is_valid()):
            return render(
                request,
                self.template_name,
                {"form": form, "formset": formset, "mode": "edit", "object": obj},
                status=400,
            )
        saved = form.save(commit=False)
        saved.headers = build_headers_from_formset(formset, existing=existing_headers)
        saved.save()
        messages.success(request, _("MCP server '%(name)s' updated.") % {"name": obj.name})
        return redirect(reverse("mcp_servers:list"))


class MCPServerDetailView(AdminRequiredMixin, View):
    http_method_names = ["get"]

    def get(self, request, name):
        obj = get_object_or_404(MCPServer, name=name)
        tools = services.discover_tools_cached(obj)
        return render(request, "mcp_servers/detail.html", {"object": obj, "tools": tools, "builtin": obj.is_builtin()})


class MCPServerDeleteView(AdminRequiredMixin, View):
    http_method_names = ["get", "post"]

    def get(self, request, name):
        obj = get_object_or_404(MCPServer, name=name, source=MCPServer.Source.CUSTOM)
        return render(request, "mcp_servers/confirm_delete.html", {"object": obj})

    def post(self, request, name):
        obj = get_object_or_404(MCPServer, name=name, source=MCPServer.Source.CUSTOM)
        obj.delete()
        messages.success(request, _("MCP server '%(name)s' deleted.") % {"name": name})
        return redirect(reverse("mcp_servers:list"))


class MCPServerToggleView(AdminRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, name):
        obj = get_object_or_404(MCPServer, name=name)
        obj.enabled = not obj.enabled
        obj.save(update_fields=["enabled", "modified"])
        return redirect(reverse("mcp_servers:list"))


class MCPServerTestView(AdminRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request):
        formset = MCPServerHeaderFormSet(request.POST, prefix="headers")
        if not formset.is_valid():
            return JsonResponse({"ok": False, "error": "invalid headers"}, status=400)
        headers = build_headers_from_formset(formset, existing=None)
        payload = {"transport": request.POST.get("transport"), "url": request.POST.get("url"), "headers": headers}
        result = async_to_sync(services.test_connection)(payload)
        return JsonResponse(result, status=200 if result.get("ok") else 502)


class MCPServerToolsView(AdminRequiredMixin, View):
    http_method_names = ["get"]

    def get(self, request, name):
        obj = get_object_or_404(MCPServer, name=name)
        return JsonResponse({"tools": services.discover_tools_cached(obj)})


def _existing_headers_for_formset(obj: MCPServer) -> tuple[list[dict], bool]:
    """Return ``(initial, headers_locked)`` for the headers formset.

    Literal values are blanked: ``build_headers_from_formset`` treats blank
    on POST as "preserve existing". ``headers_locked`` is True when the
    ciphertext can't be decoded — callers must refuse the POST so the
    recoverable ciphertext isn't overwritten with [].
    """
    try:
        rows = obj.headers or []
    except DecryptionError:
        logger.warning("Headers for MCP server %r could not be decrypted.", obj.name)
        return [], True
    out: list[dict] = []
    for h in rows:
        value = h.get("value", "")
        if h.get("mode") == "literal" and value:
            value = ""
        out.append({"name": h.get("name", ""), "mode": h.get("mode", "literal"), "value": value})
    return out, False
