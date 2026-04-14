class NotificationError(Exception):
    """Base exception for the notifications app."""


class UnknownChannelError(NotificationError):
    """Raised when a channel_type is not registered."""

    def __init__(self, channel_type: str):
        super().__init__(f"Unknown channel type: {channel_type!r}")
        self.channel_type = channel_type


class UnrecoverableDeliveryError(NotificationError):
    """Raised by Channel.send() to signal a permanent failure that must not be retried."""
