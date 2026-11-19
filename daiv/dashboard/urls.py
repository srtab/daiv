from django.urls import path

from dashboard.views import DashboardView, InsightsSpendView, InsightsView, SidebarListsView, SpendExportView

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),
    path("insights/", InsightsView.as_view(), name="insights"),
    path("insights/spend/", InsightsSpendView.as_view(), name="insights_spend"),
    path("insights/spend/export.csv", SpendExportView.as_view(), name="insights_spend_export"),
    path("sidebar/lists/", SidebarListsView.as_view(), name="sidebar_lists"),
]
