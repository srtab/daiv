from django.urls import path

from accounts.views import APIKeyCreateView, APIKeyListView, APIKeyRevokeView, DashboardView

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),
    path("api-keys/", APIKeyListView.as_view(), name="api_keys"),
    path("api-keys/create/", APIKeyCreateView.as_view(), name="api_key_create"),
    path("api-keys/<int:pk>/revoke/", APIKeyRevokeView.as_view(), name="api_key_revoke"),
]
