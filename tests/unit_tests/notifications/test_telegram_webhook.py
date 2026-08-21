from __future__ import annotations

import json
import time

from django.urls import reverse

import httpx
import pytest
from notifications.api.telegram_handlers import MSG_BARE_START, MSG_CONNECTED, MSG_LINK_EXPIRED, MSG_PRIVATE_ONLY
from notifications.choices import ChannelType
from notifications.models import UserChannelBinding
from notifications.telegram.tokens import TOKEN_TTL_SECONDS, mint_token
from notifications.telegram_bindings import bind_chat, binding_state
from pydantic import SecretStr

from accounts.models import Role, User

pytestmark = pytest.mark.django_db

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"  # noqa: S105 — a header name, not a secret
URL = "/api/notifications/callbacks/telegram/"
URL_NO_SLASH = "/api/notifications/callbacks/telegram"
SEND_MESSAGE_URL = "https://api.telegram.org/bot123:ABC/sendMessage"


def _post(client, payload, *, secret="s3cret", url=URL):  # noqa: S107 — matches the telegram_configured fixture
    headers = {} if secret is None else {SECRET_HEADER: secret}
    return client.post(url, data=json.dumps(payload), content_type="application/json", headers=headers)


def _start(token="", chat_id=555, chat_type="private", username="alice"):
    text = f"/start {token}".strip()
    return {
        "update_id": 1,
        "message": {"message_id": 1, "chat": {"id": chat_id, "type": chat_type, "username": username}, "text": text},
    }


def _stop(chat_id=555):
    return {"update_id": 2, "message": {"message_id": 2, "chat": {"id": chat_id, "type": "private"}, "text": "/stop"}}


@pytest.fixture(autouse=True)
def _swallow_replies(request, httpx_mock):
    """Every reply is a best-effort sendMessage; let them all succeed silently.

    Tests marked ``own_replies`` opt out: pytest-httpx serves the first *not yet called* matching
    response, so this blanket one would shadow whatever such a test registers.
    """
    if request.node.get_closest_marker("own_replies"):
        return
    httpx_mock.add_response(
        method="POST",
        url=SEND_MESSAGE_URL,
        json={"ok": True, "result": {"message_id": 1}},
        status_code=200,
        is_optional=True,
        is_reusable=True,
    )


def _token_for(user):
    address, verified_at = binding_state(user)
    return mint_token(user.pk, address=address, verified_at=verified_at)


def _last_reply(httpx_mock) -> dict:
    """The JSON body of the most recent sendMessage."""
    return json.loads(httpx_mock.get_requests(url=SEND_MESSAGE_URL)[-1].content)


class TestRouteRegistration:
    def test_the_canonical_reverse_is_the_trailing_slash_variant(self):
        # sync_telegram registers this exact URL with Telegram; both variants accept posts.
        assert reverse("api:telegram_callback") == URL


@pytest.mark.usefixtures("telegram_configured")
class TestSecret:
    def test_correct_secret_is_accepted(self, client, member_user):
        assert _post(client, _start(_token_for(member_user))).status_code == 204

    def test_wrong_secret_is_401(self, client, member_user):
        assert _post(client, _start(_token_for(member_user)), secret="nope").status_code == 401  # noqa: S106

    def test_missing_header_is_401(self, client, member_user):
        assert _post(client, _start(_token_for(member_user)), secret=None).status_code == 401

    def test_a_non_ascii_header_is_401_not_a_500(self, client):
        # hmac.compare_digest raises TypeError on a non-ASCII str, and every header byte above
        # 0x7F decodes into one — so comparing the str form answers a crafted header with a 500.
        assert _post(client, _stop(), secret="s3crét").status_code == 401  # noqa: S106

    def test_both_trailing_slash_variants_accept_the_post(self, client, member_user):
        assert _post(client, _start(_token_for(member_user)), url=URL_NO_SLASH).status_code == 204
        assert _post(client, _stop(), url=URL).status_code == 204


class TestBlankSecretFailsClosed:
    def test_blank_stored_secret_rejects_every_update(self, client, site_settings_override):
        # Deliberate divergence from validate_gitlab_webhook, which returns True when no secret
        # is configured. /stop carries no token, so a fail-open route would let anyone who can
        # reach it unbind users by guessing chat ids.

        site_settings_override(telegram_webhook_secret=None, telegram_enabled=True)
        assert _post(client, _stop(), secret="anything").status_code == 401  # noqa: S106
        assert _post(client, _stop(), secret=None).status_code == 401


@pytest.mark.usefixtures("telegram_configured")
class TestStart:
    def test_a_valid_token_binds_the_chat(self, client, member_user):
        assert _post(client, _start(_token_for(member_user))).status_code == 204
        binding = UserChannelBinding.objects.get(user=member_user, channel_type=ChannelType.TELEGRAM)
        assert binding.address == "555"
        assert binding.is_verified is True
        assert binding.extra_config == {"handle": "alice"}

    def test_an_expired_token_replies_without_a_4xx(self, client, member_user, httpx_mock):
        # Telegram retries non-2xx and eventually disables a webhook that keeps failing.
        stale = mint_token(member_user.pk, address="", verified_at="", now=int(time.time()) - TOKEN_TTL_SECONDS - 1)
        assert _post(client, _start(stale)).status_code == 204
        assert _last_reply(httpx_mock) == {"chat_id": 555, "text": str(MSG_LINK_EXPIRED)}
        assert not UserChannelBinding.objects.filter(channel_type=ChannelType.TELEGRAM).exists()

    def test_a_bare_start_replies_with_guidance_and_binds_nothing(self, client, httpx_mock):
        assert _post(client, _start("")).status_code == 204
        assert _last_reply(httpx_mock) == {"chat_id": 555, "text": str(MSG_BARE_START)}
        assert not UserChannelBinding.objects.filter(channel_type=ChannelType.TELEGRAM).exists()

    def test_a_group_chat_start_refuses(self, client, member_user, httpx_mock):
        # Binding a room would make it the recipient of one account's notifications.
        payload = _start(_token_for(member_user), chat_id=-1001234, chat_type="supergroup")
        assert _post(client, payload).status_code == 204
        assert _last_reply(httpx_mock) == {"chat_id": -1001234, "text": str(MSG_PRIVATE_ONLY)}
        assert not UserChannelBinding.objects.filter(channel_type=ChannelType.TELEGRAM).exists()

    def test_a_token_minted_before_a_bind_stops_working_after_it(self, client, member_user):
        token = _token_for(member_user)
        assert _post(client, _start(token)).status_code == 204
        # verified_at has moved, so replaying the same link is inert.
        UserChannelBinding.objects.filter(channel_type=ChannelType.TELEGRAM).update(address="999")
        assert _post(client, _start(token, chat_id=777)).status_code == 204
        rows = UserChannelBinding.objects.filter(channel_type=ChannelType.TELEGRAM)
        assert set(rows.values_list("address", flat=True)) == {"999"}

    def test_a_reconnect_from_a_new_chat_replaces_the_old_binding(self, client, member_user):
        bind_chat(member_user, chat_id="111", handle="old")
        assert _post(client, _start(_token_for(member_user), chat_id=222)).status_code == 204
        rows = UserChannelBinding.objects.filter(user=member_user, channel_type=ChannelType.TELEGRAM)
        assert [r.address for r in rows] == ["222"]

    def test_a_start_from_a_chat_bound_to_another_account_repoints_it(self, client, member_user):
        other = User.objects.create_user(
            username="other",
            email="other@test.com",
            password="testpass123",  # noqa: S106
            role=Role.MEMBER,
        )
        bind_chat(other, chat_id="555", handle="bob")
        assert _post(client, _start(_token_for(member_user))).status_code == 204
        rows = UserChannelBinding.objects.filter(channel_type=ChannelType.TELEGRAM, address="555")
        assert [r.user_id for r in rows] == [member_user.pk]


@pytest.mark.usefixtures("telegram_configured")
class TestStop:
    def test_stop_unbinds_the_sending_chat(self, client, member_user):
        bind_chat(member_user, chat_id="555", handle="alice")
        assert _post(client, _stop()).status_code == 204
        assert not UserChannelBinding.objects.filter(channel_type=ChannelType.TELEGRAM).exists()

    def test_stop_from_an_unbound_chat_is_a_no_op(self, client):
        assert _post(client, _stop(chat_id=999)).status_code == 204


@pytest.mark.usefixtures("telegram_configured")
class TestReplyDelivery:
    """The reply is best-effort: it must land on the happy path and never fail the update."""

    @pytest.mark.own_replies
    def test_the_confirmation_reaches_the_chat(self, client, member_user, httpx_mock):
        # A non-optional response, so a route that stopped replying fails at fixture teardown.
        httpx_mock.add_response(method="POST", url=SEND_MESSAGE_URL, json={"ok": True, "result": {"message_id": 1}})
        assert _post(client, _start(_token_for(member_user))).status_code == 204
        assert _last_reply(httpx_mock) == {"chat_id": 555, "text": str(MSG_CONNECTED)}

    @pytest.mark.own_replies
    @pytest.mark.parametrize("failure", ["transport", "http_500"])
    def test_a_failing_reply_still_lets_the_bind_land(self, client, member_user, httpx_mock, failure):
        # The broad catch in _reply is the only thing between a Bot API outage and the
        # retry-storm-then-webhook-disabled outcome. The two cases reach it as different
        # exception types: httpx raises the transport error, _tg_post wraps the 5xx.
        if failure == "transport":
            httpx_mock.add_exception(httpx.ConnectError("no route to host"), method="POST", url=SEND_MESSAGE_URL)
        else:
            httpx_mock.add_response(
                method="POST",
                url=SEND_MESSAGE_URL,
                json={"ok": False, "error_code": 500, "description": "busy"},
                status_code=500,
            )
        assert _post(client, _start(_token_for(member_user))).status_code == 204
        binding = UserChannelBinding.objects.get(user=member_user, channel_type=ChannelType.TELEGRAM)
        assert binding.address == "555"
        assert binding.is_verified is True

    @pytest.mark.own_replies
    def test_a_missing_bot_token_skips_the_reply_and_still_binds(self, client, member_user, site_settings_override):
        # No response is registered at all, so a request here fails the run.
        site_settings_override(telegram_bot_token=None)
        assert _post(client, _start(_token_for(member_user))).status_code == 204
        assert UserChannelBinding.objects.filter(user=member_user, channel_type=ChannelType.TELEGRAM).exists()

    @pytest.mark.own_replies
    def test_an_unreadable_bot_token_still_lets_the_bind_land(self, client, member_user, site_settings_override):
        # from_site_settings decrypts an encrypted field, so it can raise after the bind has
        # committed — which is why it is resolved inside the try rather than above it.
        class _Unreadable:
            def get_secret_value(self):
                raise RuntimeError("decrypt failed")

        site_settings_override(telegram_bot_token=_Unreadable())
        assert _post(client, _start(_token_for(member_user))).status_code == 204
        assert UserChannelBinding.objects.filter(user=member_user, channel_type=ChannelType.TELEGRAM).exists()


@pytest.mark.usefixtures("telegram_configured")
class TestMyChatMember:
    def test_blocking_the_bot_unbinds_immediately(self, client, member_user):
        # The disconnect gesture users actually make — nobody types /stop. Handling it here
        # unbinds now rather than at the next delivery's 403.
        bind_chat(member_user, chat_id="555", handle="alice")
        payload = {
            "update_id": 3,
            "my_chat_member": {"chat": {"id": 555, "type": "private"}, "new_chat_member": {"status": "kicked"}},
        }
        assert _post(client, payload).status_code == 204
        assert not UserChannelBinding.objects.filter(channel_type=ChannelType.TELEGRAM).exists()

    def test_an_unblock_transition_leaves_the_binding_alone(self, client, member_user):
        bind_chat(member_user, chat_id="555", handle="alice")
        payload = {
            "update_id": 4,
            "my_chat_member": {"chat": {"id": 555, "type": "private"}, "new_chat_member": {"status": "member"}},
        }
        assert _post(client, payload).status_code == 204
        assert UserChannelBinding.objects.filter(channel_type=ChannelType.TELEGRAM).exists()


@pytest.mark.usefixtures("telegram_configured")
class TestParsingDiscipline:
    def test_an_unmodelled_update_shape_is_a_silent_204_never_a_422(self, client):
        # A 422 is the retry-storm-then-auto-disable path.
        assert _post(client, {"update_id": 5, "poll_answer": {"poll_id": "p"}}).status_code == 204

    def test_a_malformed_body_is_a_silent_204(self, client):
        response = client.post(
            URL, data=b"not json", content_type="application/json", headers={SECRET_HEADER: "s3cret"}
        )
        assert response.status_code == 204

    def test_an_unknown_command_is_a_silent_204(self, client):
        payload = {"update_id": 6, "message": {"chat": {"id": 555, "type": "private"}, "text": "/wat"}}
        assert _post(client, payload).status_code == 204

    def test_plain_text_is_a_silent_204(self, client):
        payload = {"update_id": 7, "message": {"chat": {"id": 555, "type": "private"}, "text": "hello"}}
        assert _post(client, payload).status_code == 204


class TestDisabledChannel:
    def test_a_disabled_channel_answers_2xx_without_binding(self, client, member_user, site_settings_override):
        # A failed deleteWebhook must not leave a live binding surface.
        site_settings_override(telegram_enabled=False, telegram_webhook_secret=SecretStr("s3cret"))
        assert _post(client, _start(_token_for(member_user))).status_code == 204
        assert not UserChannelBinding.objects.filter(channel_type=ChannelType.TELEGRAM).exists()
