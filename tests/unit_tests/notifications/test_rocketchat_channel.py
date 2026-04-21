from __future__ import annotations

from unittest.mock import patch

from django.utils import timezone

import httpx
import pytest
from notifications.channels.registry import get_channel
from notifications.channels.rocketchat import RocketChatChannel, RocketChatPermanentError, _rc_post, verify_username
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


class TestRcPost:
    URL = "https://rc.example.com"
    USER_ID = "botid"
    TOKEN = "bottoken"  # noqa: S105 — test constant

    def test_success_returns_parsed_body(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url=f"{self.URL}/api/v1/chat.postMessage",
            json={"success": True, "message": {"_id": "m1"}},
            status_code=200,
            match_headers={"X-Auth-Token": self.TOKEN, "X-User-Id": self.USER_ID},
            match_json={"channel": "@alice", "text": "hi"},
        )
        result = _rc_post(self.URL, self.USER_ID, self.TOKEN, "chat.postMessage", {"channel": "@alice", "text": "hi"})
        assert result["success"] is True

    def test_4xx_raises_permanent_error(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url=f"{self.URL}/api/v1/chat.postMessage",
            json={"success": False, "error": "invalid-channel", "errorType": "error-invalid-channel"},
            status_code=400,
        )
        with pytest.raises(RocketChatPermanentError) as exc:
            _rc_post(self.URL, self.USER_ID, self.TOKEN, "chat.postMessage", {"channel": "@nope", "text": "hi"})
        assert "invalid-channel" in str(exc.value)

    def test_known_permanent_error_in_2xx_body_raises_permanent_error(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url=f"{self.URL}/api/v1/chat.postMessage",
            json={"success": False, "error": "user not found", "errorType": "error-user-not-found"},
            status_code=200,
        )
        with pytest.raises(RocketChatPermanentError):
            _rc_post(self.URL, self.USER_ID, self.TOKEN, "chat.postMessage", {"channel": "@nope", "text": "hi"})

    def test_5xx_raises_httpx_status_error(self, httpx_mock):
        httpx_mock.add_response(method="POST", url=f"{self.URL}/api/v1/chat.postMessage", status_code=503)
        with pytest.raises(httpx.HTTPStatusError):
            _rc_post(self.URL, self.USER_ID, self.TOKEN, "chat.postMessage", {"channel": "@alice", "text": "hi"})

    def test_unknown_failure_body_in_2xx_raises_generic_exception(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url=f"{self.URL}/api/v1/chat.postMessage",
            json={"success": False, "error": "something else"},
            status_code=200,
        )
        with pytest.raises(Exception) as exc:
            _rc_post(self.URL, self.USER_ID, self.TOKEN, "chat.postMessage", {"channel": "@alice", "text": "hi"})
        assert not isinstance(exc.value, RocketChatPermanentError)


class TestVerifyUsername:
    def test_success_returns_rc_user_id(self, httpx_mock):
        with patch("notifications.channels.rocketchat.site_settings") as s:
            s.rocketchat_url = "https://rc.example.com"
            s.rocketchat_user_id = "botid"
            s.rocketchat_auth_token.get_secret_value.return_value = "bottoken"
            httpx_mock.add_response(
                method="GET",
                url="https://rc.example.com/api/v1/users.info?username=alice",
                json={"success": True, "user": {"_id": "u1", "username": "alice"}},
                status_code=200,
                match_headers={"X-Auth-Token": "bottoken", "X-User-Id": "botid"},
            )
            rc_id, err = verify_username("alice")
        assert rc_id == "u1"
        assert err is None

    def test_user_not_found_returns_error(self, httpx_mock):
        with patch("notifications.channels.rocketchat.site_settings") as s:
            s.rocketchat_url = "https://rc.example.com"
            s.rocketchat_user_id = "botid"
            s.rocketchat_auth_token.get_secret_value.return_value = "bottoken"
            httpx_mock.add_response(
                method="GET",
                url="https://rc.example.com/api/v1/users.info?username=nope",
                json={"success": False, "error": "User not found.", "errorType": "error-user-not-found"},
                status_code=400,
            )
            rc_id, err = verify_username("nope")
        assert rc_id is None
        assert err and "not found" in err.lower()

    def test_not_configured_returns_error(self):
        with patch("notifications.channels.rocketchat.site_settings") as s:
            s.rocketchat_url = None
            rc_id, err = verify_username("alice")
        assert rc_id is None
        assert err == "Rocket Chat is not configured."
