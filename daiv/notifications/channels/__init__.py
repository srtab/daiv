"""Channel registry imports. Each channel module self-registers via @register_channel."""

from notifications.channels import discord, email, rocketchat, telegram  # noqa: F401
