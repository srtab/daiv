from __future__ import annotations

import logging

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView

from accounts.mixins import AdminRequiredMixin
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
