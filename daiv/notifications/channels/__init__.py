"""Channel registry imports. Each channel module self-registers via @register_channel."""

from notifications.channels import email, rocketchat, slack, telegram  # noqa: F401
