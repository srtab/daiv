from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from accounts.models import User
    from notifications.choices import ChannelType
    from notifications.models import Notification, NotificationDelivery


class NotificationChannel(ABC):
    """Base class for notification channels.

    Subclasses must define ``channel_type`` and ``display_name`` class variables,
    implement ``send``, and register themselves with the ``@register_channel``
    decorator from ``notifications.channels.registry``. ``resolve_address`` has a
    sensible default (most recent verified ``UserChannelBinding`` for this channel)
    that subclasses may override.
    """

    channel_type: ClassVar[ChannelType]
    display_name: ClassVar[str]

    @classmethod
    def is_enabled(cls) -> bool:
        return True

    def resolve_address(self, user: User) -> str | None:
        """Return the verified address to send to, or None if this user has no usable binding."""
        from notifications.models import UserChannelBinding

        binding = (
            UserChannelBinding.objects
            .filter(user=user, channel_type=self.channel_type, is_verified=True)
            .order_by("-modified")
            .first()
        )
        return binding.address if binding else None

    @abstractmethod
    def send(self, notification: Notification, delivery: NotificationDelivery) -> None:
        """Deliver. Raise UnrecoverableDeliveryError for permanent failures; any other exception
        is treated as transient and retried."""
