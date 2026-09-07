from django.urls import path

from notifications.choices import ChannelType
from notifications.views import (
    BellDropdownView,
    ChannelDisconnectView,
    ConnectTelegramView,
    MarkAllReadView,
    MarkNotificationReadView,
    NotificationListView,
    UpdateRocketChatBindingView,
)

app_name = "notifications"

urlpatterns = [
    path("", NotificationListView.as_view(), name="list"),
    path("bell/", BellDropdownView.as_view(), name="bell_dropdown"),
    path("<uuid:notification_id>/read/", MarkNotificationReadView.as_view(), name="mark_read"),
    path("read-all/", MarkAllReadView.as_view(), name="mark_all_read"),
    path("channels/rocketchat/", UpdateRocketChatBindingView.as_view(), name="rocketchat_connect"),
    path(
        "channels/rocketchat/delete/",
        ChannelDisconnectView.as_view(channel_type=ChannelType.ROCKETCHAT),
        name="rocketchat_disconnect",
    ),
    path("channels/telegram/", ConnectTelegramView.as_view(), name="telegram_connect"),
    path(
        "channels/telegram/delete/",
        ChannelDisconnectView.as_view(channel_type=ChannelType.TELEGRAM),
        name="telegram_disconnect",
    ),
]
