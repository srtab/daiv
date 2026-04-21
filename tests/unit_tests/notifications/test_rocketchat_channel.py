from __future__ import annotations

from notifications.channels.registry import get_channel
from notifications.choices import ChannelType


class TestRocketChatChannelRegistration:
    def test_channel_is_registered(self):
        channel = get_channel(ChannelType.ROCKETCHAT)
        assert channel.channel_type == ChannelType.ROCKETCHAT
