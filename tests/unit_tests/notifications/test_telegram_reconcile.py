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

    def test_a_dying_webhook_is_logged(self, httpx_mock, caplog):
        from notifications.telegram.config import webhook_url

        _info(
            httpx_mock,
            url=webhook_url(),
            last_error_message="Wrong response from the webhook: 401",
            pending_update_count=42,
        )
        with caplog.at_level("WARNING", logger="daiv.notifications"):
            telegram_webhook_reconcile_cron_task.func()
        assert "401" in caplog.text
        assert "42" in caplog.text

    def test_a_get_webhook_info_failure_is_logged_and_the_tick_gives_up(self, httpx_mock, caplog):
        import httpx

        httpx_mock.add_exception(httpx.ConnectError("no route"), url=INFO)
        with caplog.at_level("WARNING", logger="daiv.notifications"):
            telegram_webhook_reconcile_cron_task.func()
        assert "getWebhookInfo" in caplog.text
