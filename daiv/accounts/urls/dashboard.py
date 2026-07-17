from django.urls import path

from accounts.views import DashboardView, FeedItemView, ManagerLensView

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),
    path("manager/", ManagerLensView.as_view(), name="manager_lens"),
    path("feed/item/<uuid:run_id>/", FeedItemView.as_view(), name="feed_item"),
]
