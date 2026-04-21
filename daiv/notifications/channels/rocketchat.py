from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils.translation import gettext_lazy as _

from notifications.channels.base import NotificationChannel
from notifications.channels.registry import register_channel
from notifications.choices import ChannelType
from notifications.models import UserChannelBinding

if TYPE_CHECKING:
    from accounts.models import User
    from notifications.models import Notification, NotificationDelivery

logger = logging.getLogger("daiv.notifications")


class RocketChatPermanentError(Exception):
    """Raised when Rocket Chat returns a permanent error (4xx or a known non-retryable error code)."""


@register_channel
class RocketChatChannel(NotificationChannel):
    channel_type = ChannelType.ROCKETCHAT
    display_name = _("Rocket Chat")

    def resolve_address(self, user: User) -> str | None:
        binding = (
            UserChannelBinding.objects
            .filter(user=user, channel_type=self.channel_type, is_verified=True)
            .order_by("-modified")
            .first()
        )
        return binding.address if binding else None

    def send(self, notification: Notification, delivery: NotificationDelivery) -> None:
        raise NotImplementedError("send() implemented in a later task")
