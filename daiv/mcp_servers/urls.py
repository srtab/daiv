from django.urls import path

from mcp_servers.views import MCPServerListView

app_name = "mcp_servers"

urlpatterns = [path("", MCPServerListView.as_view(), name="list")]
