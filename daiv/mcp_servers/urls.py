from django.urls import path

from mcp_servers.views import (
    MCPServerCreateView,
    MCPServerDeleteView,
    MCPServerEditView,
    MCPServerListView,
    MCPServerRefreshToolsView,
    MCPServerTestView,
    MCPServerToggleView,
)

app_name = "mcp_servers"

urlpatterns = [
    path("", MCPServerListView.as_view(), name="list"),
    path("new/", MCPServerCreateView.as_view(), name="create"),
    path("test/", MCPServerTestView.as_view(), name="test"),
    path("<slug:name>/edit/", MCPServerEditView.as_view(), name="edit"),
    path("<slug:name>/delete/", MCPServerDeleteView.as_view(), name="delete"),
    path("<slug:name>/toggle/", MCPServerToggleView.as_view(), name="toggle"),
    path("<slug:name>/refresh-tools/", MCPServerRefreshToolsView.as_view(), name="refresh_tools"),
]
