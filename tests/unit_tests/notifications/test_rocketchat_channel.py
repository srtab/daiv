from __future__ import annotations

from django.utils import timezone

import pytest
from notifications.channels.registry import get_channel
from notifications.channels.rocketchat import RocketChatChannel
from notifications.choices import ChannelType
from notifications.models import UserChannelBinding


class TestRocketChatChannelRegistration:
    def test_channel_is_registered(self):
        channel = get_channel(ChannelType.ROCKETCHAT)
        assert channel.channel_type == ChannelType.ROCKETCHAT


@pytest.mark.django_db
class TestResolveAddress:
    def test_returns_address_when_verified_binding_exists(self, member_user):
        UserChannelBinding.objects.create(
            user=member_user,
            channel_type=ChannelType.ROCKETCHAT,
            address="alice",
            is_verified=True,
            verified_at=timezone.now(),
        )
        assert RocketChatChannel().resolve_address(member_user) == "alice"

    def test_returns_none_when_no_binding(self, member_user):
        assert RocketChatChannel().resolve_address(member_user) is None

    def test_returns_none_when_binding_is_unverified(self, member_user):
        UserChannelBinding.objects.create(
            user=member_user, channel_type=ChannelType.ROCKETCHAT, address="alice", is_verified=False
        )
        assert RocketChatChannel().resolve_address(member_user) is None
