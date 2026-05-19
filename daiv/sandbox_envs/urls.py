from django.urls import path

from sandbox_envs.views import EnvDeleteView, EnvFormView, EnvListView, EnvSetDefaultView

app_name = "sandbox_envs"

urlpatterns = [
    path("", EnvListView.as_view(), name="list"),
    path("create/", EnvFormView.as_view(), name="create"),
    path("<uuid:pk>/edit/", EnvFormView.as_view(), name="edit"),
    path("<uuid:pk>/delete/", EnvDeleteView.as_view(), name="delete"),
    path("<uuid:pk>/set-default/", EnvSetDefaultView.as_view(), name="set_default"),
]
