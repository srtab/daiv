from __future__ import annotations

import logging

from django.contrib import messages
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView

from asgiref.sync import async_to_sync

from accounts.mixins import AdminRequiredMixin
from mcp_servers import services
from mcp_servers.constants import TOOLS_CACHE_KEY, TOOLS_CACHE_TIMEOUT
from mcp_servers.forms import MCPServerForm, MCPServerHeaderFormSet, build_headers_from_formset
from mcp_servers.models import MCPServer

logger = logging.getLogger("daiv.mcp_servers")


class MCPServerListView(AdminRequiredMixin, TemplateView):
    template_name = "mcp_servers/list.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["custom_servers"] = list(
            MCPServer.objects.filter(source=MCPServer.Source.CUSTOM).select_related("created_by")
        )
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
        discovered = MCPServerDetailView._tools_or_empty(obj)
        form = MCPServerForm(instance=obj, discovered_tools=discovered or None)
        formset = MCPServerHeaderFormSet(initial=_existing_headers_for_formset(obj), prefix="headers")
        return render(request, self.template_name, {"form": form, "formset": formset, "mode": "edit", "object": obj})

    def post(self, request, name):
        obj = get_object_or_404(MCPServer, name=name)
        if obj.source == MCPServer.Source.BUILTIN:
            # Only ``enabled`` may change. Apply it directly and ignore everything else.
            obj.enabled = request.POST.get("enabled") == "on"
            obj.save(update_fields=["enabled", "modified"])
            messages.success(request, _("MCP server '%(name)s' updated.") % {"name": obj.name})
            return redirect(reverse("mcp_servers:list"))

        existing_headers = obj.headers or []
        form = MCPServerForm(request.POST, instance=obj)
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
        tools = self._tools_or_empty(obj)
        return render(request, "mcp_servers/detail.html", {"object": obj, "tools": tools})

    @staticmethod
    def _tools_or_empty(obj):
        stamp = int(obj.modified.timestamp())
        cache_key = TOOLS_CACHE_KEY.format(name=obj.name, stamp=stamp)
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        tools = async_to_sync(services.discover_tools)(obj)
        cache.set(cache_key, tools, TOOLS_CACHE_TIMEOUT)
        return tools


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
        stamp = int(obj.modified.timestamp())
        cache_key = TOOLS_CACHE_KEY.format(name=obj.name, stamp=stamp)
        cached = cache.get(cache_key)
        if cached is not None:
            return JsonResponse({"tools": cached, "cached": True})
        tools = async_to_sync(services.discover_tools)(obj)
        cache.set(cache_key, tools, TOOLS_CACHE_TIMEOUT)
        return JsonResponse({"tools": tools, "cached": False})


def _existing_headers_for_formset(obj: MCPServer) -> list[dict]:
    """Build the formset's ``initial`` data from a server's stored headers.

    Literal values are blanked out for display — the form's preserve-blank
    logic round-trips them. (Mask only if you later add a 'hint' column —
    the empty initial input means 'preserve on POST'.)
    """
    try:
        rows = obj.headers or []
    except Exception:  # noqa: BLE001 — degrade gracefully on DecryptionError
        return []
    out: list[dict] = []
    for h in rows:
        value = h.get("value", "")
        if h.get("mode") == "literal" and value:
            value = ""  # blank means "preserve" on POST
        out.append({"name": h.get("name", ""), "mode": h.get("mode", "literal"), "value": value})
    return out
