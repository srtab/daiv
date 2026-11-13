from __future__ import annotations

from notifications.choices import ChannelType


def test_telegram_is_a_channel_type():
    assert ChannelType.TELEGRAM == "telegram"
    assert ChannelType.TELEGRAM.label == "Telegram"


def test_channel_type_values_are_exactly_the_four_shipped_channels():
    # The channel-choice migrations hardcode this list; a channel added without a
    # matching AlterField would leave makemigrations dirty in CI.
    assert set(ChannelType.values) == {"email", "rocketchat", "discord", "telegram"}
