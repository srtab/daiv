from __future__ import annotations

import pytest
from notifications.tasks import telegram_webhook_reconcile_cron_task

from core.models import SiteConfiguration

pytestmark = pytest.mark.django_db

GET_ME = "https://api.telegram.org/bot123:ABC/getMe"
SET_WEBHOOK = "https://api.telegram.org/bot123:ABC/setWebhook"
INFO = "https://api.telegram.org/bot123:ABC/getWebhookInfo"


def _info(httpx_mock, **result):
    httpx_mock.add_response(method="POST", url=INFO, json={"ok": True, "result": result}, status_code=200)


def _ok(httpx_mock, url, result=True):
    httpx_mock.add_response(method="POST", url=url, json={"ok": True, "result": result}, status_code=200)


class TestGating:
    def test_disabled_channel_makes_no_calls(self, httpx_mock, site_settings_override):
        site_settings_override(telegram_enabled=False)
        telegram_webhook_reconcile_cron_task.func()
        assert httpx_mock.get_requests() == []

    def test_no_token_makes_no_calls(self, httpx_mock, site_settings_override):
        site_settings_override(telegram_enabled=True, telegram_bot_token=None)
        telegram_webhook_reconcile_cron_task.func()
        assert httpx_mock.get_requests() == []


@pytest.mark.usefixtures("telegram_configured")
class TestInSync:
    def test_an_in_sync_tick_is_a_single_get_webhook_info(self, httpx_mock):
        from notifications.telegram.config import webhook_url

        _info(httpx_mock, url=webhook_url(), pending_update_count=0)
        telegram_webhook_reconcile_cron_task.func()
        assert [str(r.url) for r in httpx_mock.get_requests()] == [INFO]

    def test_the_cron_never_touches_bindings(self, httpx_mock, member_user):
        from notifications.models import UserChannelBinding
        from notifications.telegram.config import webhook_url
        from notifications.telegram_bindings import bind_chat

        bind_chat(member_user, chat_id="555", handle="alice")
        _info(httpx_mock, url=webhook_url())
        telegram_webhook_reconcile_cron_task.func()
        assert UserChannelBinding.objects.filter(channel_type="telegram").count() == 1


class TestConvergence:
    def test_an_env_only_instance_converges_with_no_save(self, httpx_mock, monkeypatch):
        from core.site_settings import _docker_secret_cache

        monkeypatch.setenv("DAIV_TELEGRAM_BOT_TOKEN", "123:ABC")
        monkeypatch.setenv("DAIV_TELEGRAM_ENABLED", "true")
        for key in ("DAIV_TELEGRAM_BOT_TOKEN", "DAIV_TELEGRAM_ENABLED"):
            _docker_secret_cache.pop(key, None)
        SiteConfiguration._invalidate_cache()
        try:
            _info(httpx_mock, url="")  # nothing registered yet
            _ok(httpx_mock, GET_ME, {"id": 1, "username": "daiv_bot"})
            _ok(httpx_mock, SET_WEBHOOK)
            telegram_webhook_reconcile_cron_task.func()
            config = SiteConfiguration.objects.get(pk=1)
            assert config.telegram_bot_username == "daiv_bot"
            assert config.telegram_webhook_secret
        finally:
            for key in ("DAIV_TELEGRAM_BOT_TOKEN", "DAIV_TELEGRAM_ENABLED"):
                _docker_secret_cache.pop(key, None)
            SiteConfiguration._invalidate_cache()

    def test_a_blank_username_triggers_a_sync_even_when_the_url_matches(self, httpx_mock, site_settings_override):
        from notifications.telegram.config import webhook_url
        from pydantic import SecretStr

        site_settings_override(
            telegram_enabled=True,
            telegram_bot_token=SecretStr("123:ABC"),
            telegram_bot_username=None,
            telegram_webhook_secret=SecretStr("s3cret"),
        )
        _info(httpx_mock, url=webhook_url())
        _ok(httpx_mock, GET_ME, {"id": 1, "username": "daiv_bot"})
        _ok(httpx_mock, SET_WEBHOOK)
        telegram_webhook_reconcile_cron_task.func()
        assert SET_WEBHOOK in [str(r.url) for r in httpx_mock.get_requests()]


@pytest.mark.usefixtures("telegram_configured")
class TestVisibility:
    def test_a_foreign_url_is_re_registered_with_a_warning_naming_it(self, httpx_mock, caplog):
        _info(httpx_mock, url="https://staging.example.com/api/notifications/callbacks/telegram/")
        _ok(httpx_mock, GET_ME, {"id": 1, "username": "daiv_bot"})
        _ok(httpx_mock, SET_WEBHOOK)
        with caplog.at_level("WARNING", logger="daiv.notifications"):
            telegram_webhook_reconcile_cron_task.func()
        assert "staging.example.com" in caplog.text
        assert SET_WEBHOOK in [str(r.url) for r in httpx_mock.get_requests()]

    def test_a_dying_webhook_is_logged_and_re_registered(self, httpx_mock, caplog):
        # A matching URL is not enough to call the webhook healthy: the stored secret can have
        # diverged from Telegram's, which 401s every update forever and no other path repairs.
        from notifications.telegram.config import webhook_url

        _info(
            httpx_mock,
            url=webhook_url(),
            last_error_message="Wrong response from the webhook: 401",
            pending_update_count=42,
        )
        _ok(httpx_mock, GET_ME, {"id": 1, "username": "daiv_bot"})
        _ok(httpx_mock, SET_WEBHOOK)
        with caplog.at_level("WARNING", logger="daiv.notifications"):
            telegram_webhook_reconcile_cron_task.func()
        assert "401" in caplog.text
        assert "42" in caplog.text
        assert SET_WEBHOOK in [str(r.url) for r in httpx_mock.get_requests()]

    def test_a_re_registration_re_asserts_the_stored_secret(self, httpx_mock, site_settings_override):
        from notifications.telegram.config import webhook_url
        from pydantic import SecretStr

        site_settings_override(
            telegram_enabled=True,
            telegram_bot_token=SecretStr("123:ABC"),
            telegram_bot_username="daiv_bot",
            telegram_webhook_secret=SecretStr("s3cret"),
        )
        _info(httpx_mock, url=webhook_url(), last_error_message="Wrong response from the webhook: 401")
        _ok(httpx_mock, GET_ME, {"id": 1, "username": "daiv_bot"})
        httpx_mock.add_response(
            method="POST",
            url=SET_WEBHOOK,
            json={"ok": True, "result": True},
            status_code=200,
            match_json={
                "url": webhook_url(),
                "secret_token": "s3cret",
                "allowed_updates": ["message", "my_chat_member"],
            },
        )
        telegram_webhook_reconcile_cron_task.func()
        assert SET_WEBHOOK in [str(r.url) for r in httpx_mock.get_requests()]

    def test_a_healthy_webhook_still_returns_after_a_single_call(self, httpx_mock):
        from notifications.telegram.config import webhook_url

        _info(httpx_mock, url=webhook_url(), last_error_message=None, pending_update_count=0)
        telegram_webhook_reconcile_cron_task.func()
        assert [str(r.url) for r in httpx_mock.get_requests()] == [INFO]

    def test_an_unbuildable_webhook_url_warns_instead_of_killing_the_tick(self, httpx_mock, caplog, monkeypatch):
        from django.contrib.sites.models import Site

        def _boom():
            raise Site.DoesNotExist("no Site row")

        monkeypatch.setattr("notifications.telegram.config.webhook_url", _boom)
        _info(httpx_mock, url="https://old.example.com/api/notifications/callbacks/telegram/")
        with caplog.at_level("WARNING", logger="daiv.notifications"):
            telegram_webhook_reconcile_cron_task.func()
        assert "webhook URL" in caplog.text
        assert [str(r.url) for r in httpx_mock.get_requests()] == [INFO]

    def test_a_get_webhook_info_failure_is_logged_and_the_tick_gives_up(self, httpx_mock, caplog):
        import httpx

        httpx_mock.add_exception(httpx.ConnectError("no route"), url=INFO)
        with caplog.at_level("WARNING", logger="daiv.notifications"):
            telegram_webhook_reconcile_cron_task.func()
        assert "getWebhookInfo" in caplog.text

    @pytest.mark.parametrize(
        "failure",
        [
            pytest.param("transport", id="unreachable"),
            pytest.param(500, id="http_500"),
            pytest.param(401, id="revoked_token"),
        ],
    )
    def test_an_anticipated_failure_stays_below_error_so_sentry_sees_no_event(self, httpx_mock, caplog, failure):
        # This is a */15 cron and Sentry's event_level is ERROR, so at ERROR one Telegram outage
        # mints four events an hour for its whole duration. WARNING is the ceiling here.
        import httpx

        if failure == "transport":
            httpx_mock.add_exception(httpx.ConnectError("no route"), url=INFO)
        else:
            httpx_mock.add_response(
                method="POST",
                url=INFO,
                json={"ok": False, "error_code": failure, "description": "nope"},
                status_code=failure,
            )
        with caplog.at_level("DEBUG", logger="daiv.notifications"):
            telegram_webhook_reconcile_cron_task.func()
        records = [r for r in caplog.records if "getWebhookInfo failed" in r.getMessage()]
        assert records, "the failure must still be reported"
        assert [r.levelname for r in records] == ["WARNING"]
        assert not any(r.exc_info for r in records)

    def test_an_unexpected_failure_still_reaches_error_with_a_traceback(self, httpx_mock, caplog, monkeypatch):
        # The other half of the split: a real bug must not be demoted along with the outages.
        from notifications.telegram.client import TGClient

        def _boom(self):
            raise MemoryError("not a Telegram problem")

        monkeypatch.setattr(TGClient, "get_webhook_info", _boom)
        with caplog.at_level("DEBUG", logger="daiv.notifications"):
            telegram_webhook_reconcile_cron_task.func()
        records = [r for r in caplog.records if "unexpectedly" in r.getMessage()]
        assert [r.levelname for r in records] == ["ERROR"]
        assert all(r.exc_info for r in records)

    def test_a_missing_stored_secret_triggers_a_sync_even_when_the_url_matches(
        self, httpx_mock, site_settings_override
    ):
        # Fail-closed means a blank secret 401s every update forever, and the URL check alone
        # would call that healthy. This clause of `healthy` is the only thing that repairs it.
        from notifications.telegram.config import webhook_url

        site_settings_override(telegram_webhook_secret=None)
        _info(httpx_mock, url=webhook_url(), pending_update_count=0)
        _ok(httpx_mock, GET_ME, {"username": "daiv_bot"})
        _ok(httpx_mock, SET_WEBHOOK)
        telegram_webhook_reconcile_cron_task.func()
        assert SET_WEBHOOK in [str(r.url) for r in httpx_mock.get_requests()]

    def test_a_sync_warning_reaches_a_log_line(self, httpx_mock, caplog):
        # That loop is the only operator visibility into a reconcile-time Telegram failure.
        _info(httpx_mock, url="https://stale.example.com/hook")
        _ok(httpx_mock, GET_ME, {"username": "daiv_bot"})
        httpx_mock.add_response(
            method="POST",
            url=SET_WEBHOOK,
            json={"ok": False, "error_code": 400, "description": "bad webhook: HTTPS url must be provided"},
            status_code=400,
        )
        with caplog.at_level("WARNING", logger="daiv.notifications"):
            telegram_webhook_reconcile_cron_task.func()
        assert "Telegram reconcile:" in caplog.text
