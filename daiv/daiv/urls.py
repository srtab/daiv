from django.urls import include, path

from core.views import HealthCheckView
from daiv.api import api

urlpatterns = [
    path("accounts/", include("accounts.allauth_urls")),
    path("accounts/api-keys/", include("accounts.api_keys_urls")),
    path("dashboard/", include("accounts.dashboard_urls")),
    path("api/", api.urls),
    path("-/alive/", HealthCheckView.as_view(), name="health_check"),
]
