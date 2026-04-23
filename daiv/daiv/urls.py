from django.contrib.sitemaps import Sitemap
from django.contrib.sitemaps.views import sitemap
from django.urls import include, path, reverse

from mcp_server.oauth import oauth_metadata

from accounts.views import homepage
from core.views import HealthCheckView
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
    path("accounts/channels/", include("accounts.urls.channels")),
    path("accounts/users/", include("accounts.urls.users")),
    path("dashboard/", include("accounts.urls.dashboard")),
    path("dashboard/configuration/", include("core.urls.configuration")),
    path("dashboard/activity/", include("activity.urls")),
    path("dashboard/chat/", include("chat.urls")),
    path("dashboard/runs/", include("activity.urls_runs", namespace="runs")),
    path("dashboard/notifications/", include("notifications.urls")),
    path("dashboard/schedules/", include("schedules.urls")),
    path("codebase/", include("codebase.urls")),
    path("api/", api.urls),
    path("oauth/", include("oauth2_provider.urls", namespace="oauth2_provider")),
    path(".well-known/oauth-authorization-server", oauth_metadata, name="oauth_metadata"),
    path("-/alive/", HealthCheckView.as_view(), name="health_check"),
    path("sitemap.xml", sitemap, {"sitemaps": {"static": StaticSitemap}}, name="sitemap"),
]
