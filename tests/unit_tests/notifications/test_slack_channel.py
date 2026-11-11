from __future__ import annotations

import json

from django.utils import timezone

import httpx
import pytest
from notifications.channels.registry import get_channel
from notifications.channels.slack import SlackChannel, SlackPermanentError, _slack_post, _SlackClient, verify_member_id
from notifications.choices import ChannelType
from notifications.exceptions import UnrecoverableDeliveryError
from notifications.models import UserChannelBinding
from pydantic import SecretStr

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
USERS_INFO_URL = "https://slack.com/api/users.info"


class TestSlackChannelRegistration:
    def test_channel_is_registered(self):
        channel = get_channel(ChannelType.SLACK)
        assert channel.channel_type == ChannelType.SLACK


@pytest.mark.django_db
class TestResolveAddress:
    def test_returns_address_when_verified_binding_exists(self, member_user):
        UserChannelBinding.objects.create(
            user=member_user,
            channel_type=ChannelType.SLACK,
            address="U0123ABCD",
            is_verified=True,
            verified_at=timezone.now(),
        )
        assert SlackChannel().resolve_address(member_user) == "U0123ABCD"

    def test_returns_none_when_no_binding(self, member_user):
        assert SlackChannel().resolve_address(member_user) is None

    def test_returns_none_when_binding_is_unverified(self, member_user):
        UserChannelBinding.objects.create(
            user=member_user, channel_type=ChannelType.SLACK, address="U0123ABCD", is_verified=False
        )
        assert SlackChannel().resolve_address(member_user) is None


class TestSlackPost:
    CLIENT = _SlackClient(token="xoxb-test-token")  # noqa: S106 — test constant

    def test_success_returns_parsed_body(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url=POST_MESSAGE_URL,
            json={"ok": True, "ts": "1700000000.000100"},
            status_code=200,
            match_headers={"Authorization": "Bearer xoxb-test-token"},
            match_json={"channel": "U0123ABCD", "text": "hi"},
        )
        result = _slack_post(self.CLIENT, "chat.postMessage", {"channel": "U0123ABCD", "text": "hi"})
        assert result["ok"] is True

    @pytest.mark.parametrize(
        "error", ["user_not_found", "channel_not_found", "invalid_auth", "account_inactive", "not_authed"]
    )
    def test_permanent_errors_raise_permanent_error(self, httpx_mock, error):
        httpx_mock.add_response(
            method="POST", url=POST_MESSAGE_URL, json={"ok": False, "error": error}, status_code=200
        )
        with pytest.raises(SlackPermanentError) as exc:
            _slack_post(self.CLIENT, "chat.postMessage", {"channel": "U0123ABCD", "text": "hi"})
        assert error in str(exc.value)

    def test_unknown_error_in_ok_false_body_is_retryable_not_permanent(self, httpx_mock):
        """Slack error codes outside the permanent set must bubble as retryable so
        the delivery task retries rather than giving up on potentially-transient failures."""
        httpx_mock.add_response(
            method="POST", url=POST_MESSAGE_URL, json={"ok": False, "error": "ratelimited"}, status_code=200
        )
        with pytest.raises(RuntimeError) as exc:
            _slack_post(self.CLIENT, "chat.postMessage", {"channel": "U0123ABCD", "text": "hi"})
        assert not isinstance(exc.value, SlackPermanentError)
        assert "ratelimited" in str(exc.value)

    def test_5xx_raises_httpx_status_error(self, httpx_mock):
        httpx_mock.add_response(method="POST", url=POST_MESSAGE_URL, status_code=503)
        with pytest.raises(httpx.HTTPStatusError):
            _slack_post(self.CLIENT, "chat.postMessage", {"channel": "U0123ABCD", "text": "hi"})


class TestVerifyMemberId:
    def test_success_returns_member_id(self, httpx_mock, slack_configured):
        httpx_mock.add_response(
            method="POST",
            url=USERS_INFO_URL,
            json={"ok": True, "user": {"id": "U0123ABCD", "name": "alice"}},
            status_code=200,
            match_headers={"Authorization": "Bearer xoxb-test-token"},
            match_json={"user": "U0123ABCD"},
        )
        member_id, err = verify_member_id("U0123ABCD")
        assert member_id == "U0123ABCD"
        assert err is None

    def test_user_not_found_returns_error(self, httpx_mock, slack_configured):
        httpx_mock.add_response(
            method="POST", url=USERS_INFO_URL, json={"ok": False, "error": "user_not_found"}, status_code=200
        )
        member_id, err = verify_member_id("U0000NOPE")
        assert member_id is None
        assert err and "not found" in err.lower()

    def test_rejected_bot_token_reports_not_configured(self, httpx_mock, slack_configured):
        httpx_mock.add_response(
            method="POST", url=USERS_INFO_URL, json={"ok": False, "error": "invalid_auth"}, status_code=200
        )
        member_id, err = verify_member_id("U0123ABCD")
        assert member_id is None
        assert err == "Slack is not configured."

    def test_not_configured_returns_error(self, monkeypatch):
        from core.site_settings import site_settings

        monkeypatch.setattr(site_settings, "slack_bot_token", None)
        member_id, err = verify_member_id("U0123ABCD")
        assert member_id is None
        assert err == "Slack is not configured."

    def test_transport_error_returns_generic_unavailable(self, httpx_mock, slack_configured):
        httpx_mock.add_exception(httpx.ConnectError("connection refused"))
        member_id, err = verify_member_id("U0123ABCD")
        assert member_id is None
        assert err == "Slack is temporarily unavailable. Please try again."

    def test_non_json_response_returns_generic_invalid(self, httpx_mock, slack_configured):
        httpx_mock.add_response(method="POST", url=USERS_INFO_URL, status_code=200, content=b"<html>oops</html>")
        member_id, err = verify_member_id("U0123ABCD")
        assert member_id is None
        assert err == "Slack returned an invalid response."

    def test_server_error_returns_generic_unavailable(self, httpx_mock, slack_configured):
        httpx_mock.add_response(method="POST", url=USERS_INFO_URL, status_code=503)
        member_id, err = verify_member_id("U0123ABCD")
        assert member_id is None
        assert err == "Slack is temporarily unavailable. Please try again."


@pytest.mark.django_db
class TestSend:
    def test_happy_path_posts_renderer_payload_as_dm(self, httpx_mock, notification_with_delivery, slack_configured):
        # event_type="schedule.finished" on the fixture → renders the schedule-finished rows.
        n, d = notification_with_delivery
        d.channel_type = ChannelType.SLACK
        d.address = "U0123ABCD"
        d.save()
        n.subject, n.body, n.link_url = "Subject", "Body line", "/x/"
        n.context = {"is_successful": True, "repo_id": "acme/api", "trigger_owner": "alice", "duration_seconds": 47}
        n.save()

        httpx_mock.add_response(method="POST", url=POST_MESSAGE_URL, json={"ok": True}, status_code=200)
        SlackChannel().send(n, d)

        request = httpx_mock.get_requests()[0]
        body = json.loads(request.content)
        # The member ID as channel is what makes chat.postMessage a DM from the bot.
        assert body["channel"] == "U0123ABCD"
        # `text` is the OS-notification line; structured detail lives in attachments.
        assert "Subject" in body["text"]
        assert body["attachments"][0]["title"] == "Subject"
        assert body["attachments"][0]["color"]  # color is set per outcome
        # `Body line` is intentionally NOT in the payload — renderer surfaces structured fields.
        assert "Body line" not in json.dumps(body)

    def test_unknown_event_type_falls_back_to_plain_text(
        self, httpx_mock, notification_with_delivery, slack_configured
    ):
        n, d = notification_with_delivery
        d.channel_type = ChannelType.SLACK
        d.address = "U0123ABCD"
        d.save()
        n.event_type = "some.future.event"  # no renderer registered
        n.subject, n.body, n.link_url = "Subject", "Body line", "/x/"
        n.save()

        httpx_mock.add_response(method="POST", url=POST_MESSAGE_URL, json={"ok": True}, status_code=200)
        SlackChannel().send(n, d)

        body = json.loads(httpx_mock.get_requests()[0].content)
        assert body["channel"] == "U0123ABCD"
        assert "Subject" in body["text"] and "Body line" in body["text"]
        assert "attachments" not in body

    def test_missing_configuration_raises_unrecoverable(
        self, notification_with_delivery, slack_configured, monkeypatch
    ):
        from core.site_settings import site_settings

        monkeypatch.setattr(site_settings, "slack_bot_token", None)
        n, d = notification_with_delivery
        d.channel_type = ChannelType.SLACK
        d.address = "U0123ABCD"
        d.save()
        with pytest.raises(UnrecoverableDeliveryError) as exc:
            SlackChannel().send(n, d)
        assert "not configured" in str(exc.value)

    def test_disabled_channel_raises_unrecoverable(self, notification_with_delivery, slack_configured, monkeypatch):
        from core.site_settings import site_settings

        monkeypatch.setattr(site_settings, "slack_enabled", False)
        n, d = notification_with_delivery
        d.channel_type = ChannelType.SLACK
        d.address = "U0123ABCD"
        d.save()
        with pytest.raises(UnrecoverableDeliveryError) as exc:
            SlackChannel().send(n, d)
        assert "disabled" in str(exc.value)

    def test_job_batch_finished_renders_repo_breakdown_on_the_wire(
        self, httpx_mock, notification_with_delivery, slack_configured
    ):
        # End-to-end pin, like Rocket Chat's: the batch rollup's per-repo breakdown
        # must reach the payload, catching any EVENT_ROWS wiring regression.
        from notifications.choices import EventType

        n, d = notification_with_delivery
        d.channel_type = ChannelType.SLACK
        d.address = "U0123ABCD"
        d.save()
        n.event_type = EventType.JOB_BATCH_FINISHED
        n.subject = "'nightly' batch: 1/2 need a look — alice"
        n.context = {
            "found_count": 1,
            "needs_attention_count": 0,
            "failed_count": 0,
            "all_clear_count": 1,
            "notable_count": 1,
            "total": 2,
            "duration_seconds": 90,
            "trigger_owner": "alice",
            "repo_ids": ["acme/api", "acme/legacy"],
        }
        n.save()

        httpx_mock.add_response(method="POST", url=POST_MESSAGE_URL, json={"ok": True}, status_code=200)
        SlackChannel().send(n, d)

        body = json.loads(httpx_mock.get_requests()[0].content)
        fields_by_title = {f["title"]: f["value"] for f in body["attachments"][0]["fields"]}
        assert "acme/api" in fields_by_title["Repositories"]
        assert "acme/legacy" in fields_by_title["Repositories"]

    def test_permanent_slack_error_raises_unrecoverable(self, httpx_mock, notification_with_delivery, slack_configured):
        n, d = notification_with_delivery
        d.channel_type = ChannelType.SLACK
        d.address = "U0000NOPE"
        d.save()
        httpx_mock.add_response(
            method="POST", url=POST_MESSAGE_URL, json={"ok": False, "error": "user_not_found"}, status_code=200
        )
        with pytest.raises(UnrecoverableDeliveryError):
            SlackChannel().send(n, d)

    def test_5xx_propagates_for_retry(self, httpx_mock, notification_with_delivery, slack_configured):
        n, d = notification_with_delivery
        d.channel_type = ChannelType.SLACK
        d.address = "U0123ABCD"
        d.save()
        httpx_mock.add_response(method="POST", url=POST_MESSAGE_URL, status_code=503)
        with pytest.raises(httpx.HTTPStatusError):
            SlackChannel().send(n, d)


class TestSlackClientFromSiteSettings:
    def test_returns_none_when_token_missing(self, monkeypatch):
        from core.site_settings import site_settings

        monkeypatch.setattr(site_settings, "slack_bot_token", None)
        assert _SlackClient.from_site_settings() is None

    def test_returns_client_when_token_present(self, slack_configured):
        client = _SlackClient.from_site_settings()
        assert client is not None
        assert client.token == "xoxb-test-token"  # noqa: S105 — test constant

    def test_secret_str_token_is_unwrapped(self, site_settings_override):
        site_settings_override(slack_enabled=True, slack_bot_token=SecretStr("xoxb-wrapped"))
        client = _SlackClient.from_site_settings()
        assert client is not None
        assert client.token == "xoxb-wrapped"  # noqa: S105 — test constant
