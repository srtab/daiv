from django.urls import path

from accounts.views import DashboardView, ManagerLensView

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),
    path("manager/", ManagerLensView.as_view(), name="manager_lens"),
]
