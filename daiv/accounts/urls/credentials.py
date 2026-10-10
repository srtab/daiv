from django.urls import path

from accounts.views import PlatformCredentialReconnectView, PlatformCredentialRevokeView, PlatformCredentialView

urlpatterns = [
    path("", PlatformCredentialView.as_view(), name="platform_credential"),
    path("revoke/", PlatformCredentialRevokeView.as_view(), name="platform_credential_revoke"),
    path("reconnect/", PlatformCredentialReconnectView.as_view(), name="platform_credential_reconnect"),
]
