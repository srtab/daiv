from django.urls import path

from accounts.views import (
    DashboardView,
    FeedItemFixView,
    FeedItemSeenView,
    FeedItemView,
    FeedUnreadBadgeView,
    ManagerLensView,
)

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),
    path("manager/", ManagerLensView.as_view(), name="manager_lens"),
    path("feed/item/<uuid:run_id>/", FeedItemView.as_view(), name="feed_item"),
    path("feed/item/<uuid:run_id>/seen/", FeedItemSeenView.as_view(), name="feed_item_seen"),
    path("feed/item/<uuid:run_id>/fix/", FeedItemFixView.as_view(), name="feed_item_fix"),
    path("feed/unread-badge/", FeedUnreadBadgeView.as_view(), name="feed_unread_badge"),
]
