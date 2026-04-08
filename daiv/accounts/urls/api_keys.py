from django.urls import path

from accounts.views import APIKeyCreateView, APIKeyListView, APIKeyRevokeView

urlpatterns = [
    path("", APIKeyListView.as_view(), name="api_keys"),
    path("create/", APIKeyCreateView.as_view(), name="api_key_create"),
    path("<int:pk>/revoke/", APIKeyRevokeView.as_view(), name="api_key_revoke"),
]
