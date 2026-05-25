from __future__ import annotations

import logging

from django.views.generic import TemplateView

from accounts.mixins import AdminRequiredMixin
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
