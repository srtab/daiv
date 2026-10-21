from django.contrib.sitemaps import Sitemap
from django.contrib.sitemaps.views import sitemap
from django.urls import include, path, reverse

from dashboard.views import homepage
from mcp_api.oauth import oauth_metadata
from notifications.views import UserChannelsView

from core.views import HealthCheckView, VersionView
from daiv.api import api


class StaticSitemap(Sitemap):
    changefreq = "weekly"

    def items(self):
        return ["homepage"]

    def location(self, item):
        return reverse(item)


urlpatterns = [
    path("", homepage, name="homepage"),
    path("accounts/", include("accounts.urls.allauth")),
    path("accounts/api-keys/", include("accounts.urls.api_keys")),
    path("accounts/channels/", UserChannelsView.as_view(), name="user_channels"),
    path("accounts/users/", include("accounts.urls.users")),
    path("dashboard/", include("dashboard.urls")),
    path("dashboard/configuration/", include("configuration.urls")),
    path("dashboard/sessions/", include("sessions.urls")),
    path("dashboard/artifacts/", include("sessions.urls_artifacts")),
    path("dashboard/runs/", include("sessions.urls_runs", namespace="runs")),
    path("dashboard/notifications/", include("notifications.urls")),
    path("dashboard/sandbox-envs/", include("sandbox_envs.urls", namespace="sandbox_envs")),
    path("dashboard/schedules/", include("schedules.urls")),
    path("dashboard/memory/", include("memory.urls", namespace="memory")),
    path("dashboard/skills/", include("skills.urls", namespace="skills")),
    path("dashboard/mcp-servers/", include("mcp_connectors.urls", namespace="mcp_connectors")),
    path("codebase/", include("codebase.urls")),
    path("api/", api.urls),
    path("oauth/", include("oauth2_provider.urls", namespace="oauth2_provider")),
    path(".well-known/oauth-authorization-server", oauth_metadata, name="oauth_metadata"),
    path("-/alive/", HealthCheckView.as_view(), name="health_check"),
    path("-/version/", VersionView.as_view(), name="version"),
    path("sitemap.xml", sitemap, {"sitemaps": {"static": StaticSitemap}}, name="sitemap"),
]
