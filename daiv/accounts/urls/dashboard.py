from django.urls import path

from accounts.views import DashboardView

urlpatterns = [path("", DashboardView.as_view(), name="dashboard")]
