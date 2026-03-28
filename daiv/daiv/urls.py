from django.urls import include, path

from accounts.socialaccount import oauth2_callback, oauth2_login
from core.views import HealthCheckView
from daiv.api import api

urlpatterns = [
    # Custom GitLab OAuth views must come before allauth.urls so they take
    # precedence over the default GitLab provider views.  This lets us use a
    # Docker-internal URL for server-side token exchange while keeping the
    # browser-facing authorize URL unchanged.
    path("accounts/gitlab/login/", oauth2_login, name="gitlab_login"),
    path("accounts/gitlab/login/callback/", oauth2_callback, name="gitlab_callback"),
    path("accounts/", include("allauth.urls")),
    path("dashboard/", include("accounts.urls")),
    path("api/", api.urls),
    path("-/alive/", HealthCheckView.as_view(), name="health_check"),
]
