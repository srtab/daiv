from __future__ import annotations

from django.utils import timezone

import pytest
from notifications.choices import ChannelType
from notifications.models import UserChannelBinding
from notifications.telegram_bindings import bind_chat, binding_state_for_pk, unbind_chat, unverify_binding

from accounts.models import Role, User

pytestmark = pytest.mark.django_db


def _other_user():
    return User.objects.create_user(
        username="other",
        email="other@test.com",
        password="testpass123",  # noqa: S106
        role=Role.MEMBER,
    )


class TestBindingState:
    def test_empty_pair_when_no_binding_exists(self):
        # Both fold as empty strings so mint and verify resolve them identically.
        assert binding_state_for_pk(_other_user().pk) == ("", "")

    def test_returns_the_address_and_an_iso_timestamp(self, member_user):
        bind_chat(member_user, chat_id="555", handle="alice")
        address, verified_at = binding_state_for_pk(member_user.pk)
        assert address == "555"
        assert verified_at.startswith(str(timezone.now().year))

    def test_by_pk_for_an_unknown_pk_is_the_empty_pair(self):
        assert binding_state_for_pk(9_999_999) == ("", "")


class TestBindChat:
    def test_creates_a_verified_binding_carrying_the_handle(self, member_user):
        bind_chat(member_user, chat_id="555", handle="alice")
        binding = UserChannelBinding.objects.get(user=member_user, channel_type=ChannelType.TELEGRAM)
        assert binding.address == "555"
        assert binding.is_verified is True
        assert binding.verified_at is not None
        assert binding.extra_config == {"handle": "alice"}

    def test_is_idempotent_so_a_redelivered_update_is_absorbed(self, member_user):
        bind_chat(member_user, chat_id="555", handle="alice")
        bind_chat(member_user, chat_id="555", handle="alice")
        assert UserChannelBinding.objects.filter(user=member_user, channel_type=ChannelType.TELEGRAM).count() == 1

    def test_at_most_one_telegram_binding_per_user(self, member_user):
        bind_chat(member_user, chat_id="555", handle="alice")
        bind_chat(member_user, chat_id="666", handle="alice2")
        rows = UserChannelBinding.objects.filter(user=member_user, channel_type=ChannelType.TELEGRAM)
        assert [r.address for r in rows] == ["666"]

    def test_at_most_one_user_per_chat_id(self, member_user):
        other = _other_user()
        bind_chat(other, chat_id="555", handle="bob")
        bind_chat(member_user, chat_id="555", handle="alice")
        rows = UserChannelBinding.objects.filter(channel_type=ChannelType.TELEGRAM, address="555")
        assert [r.user_id for r in rows] == [member_user.pk]

    def test_does_not_touch_other_channels(self, member_user, email_binding):
        bind_chat(member_user, chat_id="555", handle="alice")
        assert UserChannelBinding.objects.filter(user=member_user, channel_type=ChannelType.EMAIL).exists()


class TestUnbindChat:
    def test_removes_the_sending_chats_binding_and_reports_the_count(self, member_user):
        bind_chat(member_user, chat_id="555", handle="alice")
        assert unbind_chat("555") == 1
        assert not UserChannelBinding.objects.filter(channel_type=ChannelType.TELEGRAM).exists()

    def test_unknown_chat_id_is_a_no_op(self):
        assert unbind_chat("nope") == 0


class TestUnverifyBinding:
    def test_flips_is_verified_and_leaves_the_stale_timestamp(self, member_user):
        # The user_channel_binding_verified_has_timestamp CheckConstraint permits
        # is_verified=False alongside a stale verified_at, so no timestamp juggling is needed.
        bind_chat(member_user, chat_id="555", handle="alice")
        assert unverify_binding(member_user.pk, "555") == 1
        binding = UserChannelBinding.objects.get(user=member_user, channel_type=ChannelType.TELEGRAM)
        assert binding.is_verified is False
        assert binding.verified_at is not None

    def test_is_idempotent(self, member_user):
        bind_chat(member_user, chat_id="555", handle="alice")
        unverify_binding(member_user.pk, "555")
        assert unverify_binding(member_user.pk, "555") == 0
