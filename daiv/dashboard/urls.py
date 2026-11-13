from django.urls import path

from dashboard.views import DashboardView, InsightsView, SidebarListsView

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),
    path("insights/", InsightsView.as_view(), name="insights"),
    path("sidebar/lists/", SidebarListsView.as_view(), name="sidebar_lists"),
]
