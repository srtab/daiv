from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from accounts.models import User
    from notifications.models import Notification, NotificationDelivery


class NotificationChannel(ABC):
    """Base class for notification channels. Subclasses register via @register_channel."""

    channel_type: ClassVar[str]
    display_name: ClassVar[str]

    @abstractmethod
    def resolve_address(self, user: User) -> str | None:
        """Return the address to send to, or None if this user has no binding for this channel."""

    @abstractmethod
    def send(self, notification: Notification, delivery: NotificationDelivery) -> None:
        """Deliver. Raise UnrecoverableDeliveryError for permanent failures; any other exception
        is treated as transient and retried."""
