from django.urls import path

from notifications.views import (
    BellBadgeView,
    BellDropdownView,
    MarkAllReadView,
    MarkNotificationReadView,
    NotificationListView,
)

app_name = "notifications"

urlpatterns = [
    path("", NotificationListView.as_view(), name="list"),
    path("bell/", BellDropdownView.as_view(), name="bell_dropdown"),
    path("bell/badge/", BellBadgeView.as_view(), name="bell_badge"),
    path("<uuid:notification_id>/read/", MarkNotificationReadView.as_view(), name="mark_read"),
    path("read-all/", MarkAllReadView.as_view(), name="mark_all_read"),
]
