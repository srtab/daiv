from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from django.utils import timezone

import httpx
import pytest
from notifications.channels.registry import get_channel
from notifications.channels.rocketchat import (
    RocketChatChannel,
    RocketChatPermanentError,
    _compose_text,
    _extract_rc_error,
    _rc_post,
    _RCClient,
    verify_username,
)
from notifications.choices import ChannelType
from notifications.exceptions import UnrecoverableDeliveryError
from notifications.models import UserChannelBinding
from pydantic import SecretStr


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
    CLIENT = _RCClient(url="https://rc.example.com", user_id="botid", token="bottoken")  # noqa: S106 — test constant
    ENDPOINT = "https://rc.example.com/api/v1/chat.postMessage"

    def test_success_returns_parsed_body(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url=self.ENDPOINT,
            json={"success": True, "message": {"_id": "m1"}},
            status_code=200,
            match_headers={"X-Auth-Token": self.CLIENT.token, "X-User-Id": self.CLIENT.user_id},
            match_json={"channel": "@alice", "text": "hi"},
        )
        result = _rc_post(self.CLIENT, "chat.postMessage", {"channel": "@alice", "text": "hi"})
        assert result["success"] is True

    def test_4xx_raises_permanent_error(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url=self.ENDPOINT,
            json={"success": False, "error": "invalid-channel", "errorType": "error-invalid-channel"},
            status_code=400,
        )
        with pytest.raises(RocketChatPermanentError) as exc:
            _rc_post(self.CLIENT, "chat.postMessage", {"channel": "@nope", "text": "hi"})
        assert "invalid-channel" in str(exc.value)

    def test_known_permanent_error_in_2xx_body_raises_permanent_error(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url=self.ENDPOINT,
            json={"success": False, "error": "user not found", "errorType": "error-user-not-found"},
            status_code=200,
        )
        with pytest.raises(RocketChatPermanentError):
            _rc_post(self.CLIENT, "chat.postMessage", {"channel": "@nope", "text": "hi"})

    def test_5xx_raises_httpx_status_error(self, httpx_mock):
        httpx_mock.add_response(method="POST", url=self.ENDPOINT, status_code=503)
        with pytest.raises(httpx.HTTPStatusError):
            _rc_post(self.CLIENT, "chat.postMessage", {"channel": "@alice", "text": "hi"})

    def test_unknown_errortype_in_2xx_is_retryable_not_permanent(self, httpx_mock):
        """RC errorTypes not in _PERMANENT_ERROR_TYPES must bubble as retryable so
        the delivery task retries rather than giving up on potentially-transient failures."""
        httpx_mock.add_response(
            method="POST",
            url=self.ENDPOINT,
            json={"success": False, "error": "Too many requests.", "errorType": "error-too-many-requests"},
            status_code=200,
        )
        with pytest.raises(RuntimeError) as exc:
            _rc_post(self.CLIENT, "chat.postMessage", {"channel": "@alice", "text": "hi"})
        assert not isinstance(exc.value, RocketChatPermanentError)
        assert "error-too-many-requests" in str(exc.value)


class TestVerifyUsername:
    def test_success_returns_rc_user_id(self, httpx_mock, rocketchat_configured):
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

    def test_user_not_found_returns_error(self, httpx_mock, rocketchat_configured):
        httpx_mock.add_response(
            method="GET",
            url="https://rc.example.com/api/v1/users.info?username=nope",
            json={"success": False, "error": "User not found.", "errorType": "error-user-not-found"},
            status_code=400,
        )
        rc_id, err = verify_username("nope")
        assert rc_id is None
        assert err and "not found" in err.lower()

    def test_not_configured_returns_error(self, monkeypatch):
        from core.site_settings import site_settings

        monkeypatch.setattr(site_settings, "rocketchat_url", None)
        rc_id, err = verify_username("alice")
        assert rc_id is None
        assert err == "Rocket Chat is not configured."

    def test_transport_error_returns_generic_unavailable(self, httpx_mock, rocketchat_configured, monkeypatch):
        from core.site_settings import site_settings

        monkeypatch.setattr(site_settings, "rocketchat_url", "https://rc.internal:3000")
        httpx_mock.add_exception(httpx.ConnectError("connection refused to rc.internal:3000"))
        rc_id, err = verify_username("alice")
        assert rc_id is None
        # Must NOT leak the raw exception or internal URL
        assert err == "Rocket Chat is temporarily unavailable. Please try again."
        assert "rc.internal" not in (err or "")

    def test_non_json_response_returns_generic_invalid(self, httpx_mock, rocketchat_configured):
        httpx_mock.add_response(
            method="GET",
            url="https://rc.example.com/api/v1/users.info?username=alice",
            status_code=200,
            content=b"<html>oops</html>",
        )
        rc_id, err = verify_username("alice")
        assert rc_id is None
        assert err == "Rocket Chat returned an invalid response."

    def test_server_error_returns_generic_unavailable(self, httpx_mock, rocketchat_configured):
        httpx_mock.add_response(
            method="GET", url="https://rc.example.com/api/v1/users.info?username=alice", status_code=503
        )
        rc_id, err = verify_username("alice")
        assert rc_id is None
        assert err == "Rocket Chat is temporarily unavailable. Please try again."


@pytest.mark.django_db
class TestSend:
    def test_happy_path_posts_to_user_dm(self, httpx_mock, notification_with_delivery, rocketchat_configured):
        n, d = notification_with_delivery
        d.channel_type = ChannelType.ROCKETCHAT
        d.address = "alice"
        d.save()
        n.subject, n.body, n.link_url = "Subject", "Body line", "/x/"
        n.save()

        httpx_mock.add_response(
            method="POST", url="https://rc.example.com/api/v1/chat.postMessage", json={"success": True}, status_code=200
        )
        RocketChatChannel().send(n, d)

        request = httpx_mock.get_requests()[0]
        body = json.loads(request.content)
        assert body["channel"] == "@alice"
        assert "Subject" in body["text"] and "Body line" in body["text"]

    def test_missing_configuration_raises_unrecoverable(
        self, notification_with_delivery, rocketchat_configured, monkeypatch
    ):
        from core.site_settings import site_settings

        monkeypatch.setattr(site_settings, "rocketchat_url", None)
        n, d = notification_with_delivery
        d.channel_type = ChannelType.ROCKETCHAT
        d.address = "alice"
        d.save()
        with pytest.raises(UnrecoverableDeliveryError) as exc:
            RocketChatChannel().send(n, d)
        assert "not configured" in str(exc.value)

    def test_permanent_rc_error_raises_unrecoverable(
        self, httpx_mock, notification_with_delivery, rocketchat_configured
    ):
        n, d = notification_with_delivery
        d.channel_type = ChannelType.ROCKETCHAT
        d.address = "nope"
        d.save()
        httpx_mock.add_response(
            method="POST",
            url="https://rc.example.com/api/v1/chat.postMessage",
            json={"success": False, "error": "User not found.", "errorType": "error-user-not-found"},
            status_code=200,
        )
        with pytest.raises(UnrecoverableDeliveryError):
            RocketChatChannel().send(n, d)

    def test_5xx_propagates_for_retry(self, httpx_mock, notification_with_delivery, rocketchat_configured):
        n, d = notification_with_delivery
        d.channel_type = ChannelType.ROCKETCHAT
        d.address = "alice"
        d.save()
        httpx_mock.add_response(method="POST", url="https://rc.example.com/api/v1/chat.postMessage", status_code=503)
        with pytest.raises(httpx.HTTPStatusError):
            RocketChatChannel().send(n, d)


class TestComposeText:
    def _notif(self, subject, body, link_url=""):
        return SimpleNamespace(subject=subject, body=body, link_url=link_url)

    def test_without_link_url(self):
        result = _compose_text(self._notif("Subject", "Body"))
        assert result == "Subject\n\nBody"

    def test_with_link_url_appends_absolute_url(self):
        with patch("notifications.channels.rocketchat.build_absolute_url", return_value="https://daiv.test/x/"):
            result = _compose_text(self._notif("Subject", "Body", "/x/"))
        assert result == "Subject\n\nBody\n\nhttps://daiv.test/x/"


class TestExtractRcError:
    def test_prefers_body_error(self):
        response = httpx.Response(status_code=400, json={"error": "nope", "errorType": "error-x"})
        assert _extract_rc_error(response) == "nope"

    def test_falls_back_to_error_type(self):
        response = httpx.Response(status_code=400, json={"errorType": "error-x"})
        assert _extract_rc_error(response) == "error-x"

    def test_falls_back_to_default(self):
        response = httpx.Response(status_code=500, json={})
        assert _extract_rc_error(response, default="custom default") == "custom default"

    def test_falls_back_to_http_code_when_no_default(self):
        response = httpx.Response(status_code=500, json={})
        assert _extract_rc_error(response) == "HTTP 500"

    def test_non_json_body_uses_default(self):
        response = httpx.Response(status_code=500, content=b"<html>oops</html>")
        assert _extract_rc_error(response) == "HTTP 500"


class TestRCClientFromSiteSettings:
    @pytest.mark.parametrize(
        "url,user_id,token", [(None, "u", "t"), ("https://rc", None, "t"), ("https://rc", "u", None), ("", "u", "t")]
    )
    def test_returns_none_when_any_credential_missing(self, monkeypatch, url, user_id, token):
        from core.site_settings import site_settings

        monkeypatch.setattr(site_settings, "rocketchat_url", url)
        monkeypatch.setattr(site_settings, "rocketchat_user_id", user_id)
        monkeypatch.setattr(site_settings, "rocketchat_auth_token", SecretStr(token) if token else None)
        assert _RCClient.from_site_settings() is None

    def test_returns_client_when_all_present(self, rocketchat_configured):
        client = _RCClient.from_site_settings()
        assert client is not None
        assert client.url == "https://rc.example.com"
        assert client.user_id == "botid"
        assert client.token == "bottoken"  # noqa: S105 — test constant
