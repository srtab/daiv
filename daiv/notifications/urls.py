from django.urls import path

from notifications.views import (
    BellBadgeView,
    BellDropdownView,
    DeleteRocketChatBindingView,
    MarkAllReadView,
    MarkNotificationReadView,
    NotificationListView,
    UpdateRocketChatBindingView,
)

app_name = "notifications"

urlpatterns = [
    path("", NotificationListView.as_view(), name="list"),
    path("bell/", BellDropdownView.as_view(), name="bell_dropdown"),
    path("bell/badge/", BellBadgeView.as_view(), name="bell_badge"),
    path("<uuid:notification_id>/read/", MarkNotificationReadView.as_view(), name="mark_read"),
    path("read-all/", MarkAllReadView.as_view(), name="mark_all_read"),
    path("channels/rocketchat/", UpdateRocketChatBindingView.as_view(), name="rocketchat_connect"),
    path("channels/rocketchat/delete/", DeleteRocketChatBindingView.as_view(), name="rocketchat_disconnect"),
]
