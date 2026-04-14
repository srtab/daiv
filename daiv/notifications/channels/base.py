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
    implement ``resolve_address`` and ``send``, and register themselves with the
    ``@register_channel`` decorator from ``notifications.channels.registry``.
    """

    channel_type: ClassVar[ChannelType]
    display_name: ClassVar[str]

    @abstractmethod
    def resolve_address(self, user: User) -> str | None:
        """Return the verified address to send to, or None if this user has no usable binding."""

    @abstractmethod
    def send(self, notification: Notification, delivery: NotificationDelivery) -> None:
        """Deliver. Raise UnrecoverableDeliveryError for permanent failures; any other exception
        is treated as transient and retried."""
