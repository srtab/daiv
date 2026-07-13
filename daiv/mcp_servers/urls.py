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
    path("<int:pk>/edit/", MCPServerEditView.as_view(), name="edit"),
    path("<int:pk>/delete/", MCPServerDeleteView.as_view(), name="delete"),
    path("<int:pk>/toggle/", MCPServerToggleView.as_view(), name="toggle"),
    path("<int:pk>/refresh-tools/", MCPServerRefreshToolsView.as_view(), name="refresh_tools"),
]
