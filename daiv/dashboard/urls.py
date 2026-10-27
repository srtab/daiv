from django.urls import path

from dashboard.views import DashboardView, InsightsView

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),
    path("insights/", InsightsView.as_view(), name="insights"),
]
