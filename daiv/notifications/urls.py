from django.urls import path

from notifications.choices import ChannelType
from notifications.forms import RocketChatBindingForm, SlackBindingForm
from notifications.views import (
    ChannelDisconnectView,
    ConnectTelegramView,
    MarkAllReadView,
    MarkNotificationReadView,
    NotificationListView,
    UpdateChannelBindingView,
)

app_name = "notifications"

urlpatterns = [
    path("", NotificationListView.as_view(), name="list"),
    path("<uuid:notification_id>/read/", MarkNotificationReadView.as_view(), name="mark_read"),
    path("read-all/", MarkAllReadView.as_view(), name="mark_all_read"),
    path(
        "channels/rocketchat/",
        UpdateChannelBindingView.as_view(channel_type=ChannelType.ROCKETCHAT, form_class=RocketChatBindingForm),
        name="rocketchat_connect",
    ),
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
    path(
        "channels/slack/",
        UpdateChannelBindingView.as_view(channel_type=ChannelType.SLACK, form_class=SlackBindingForm),
        name="slack_connect",
    ),
    path(
        "channels/slack/delete/", ChannelDisconnectView.as_view(channel_type=ChannelType.SLACK), name="slack_disconnect"
    ),
]
