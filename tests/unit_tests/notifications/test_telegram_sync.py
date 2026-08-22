from __future__ import annotations

import pytest
from notifications.telegram.config import sync_telegram, webhook_url

from core.models import SiteConfiguration

pytestmark = pytest.mark.django_db

GET_ME = "https://api.telegram.org/bot123:ABC/getMe"
SET_WEBHOOK = "https://api.telegram.org/bot123:ABC/setWebhook"
DELETE_WEBHOOK = "https://api.telegram.org/bot123:ABC/deleteWebhook"


def _ok_get_me(httpx_mock, username="daiv_bot"):
    httpx_mock.add_response(
        method="POST", url=GET_ME, json={"ok": True, "result": {"id": 1, "username": username}}, status_code=200
    )


def _ok_set_webhook(httpx_mock):
    httpx_mock.add_response(method="POST", url=SET_WEBHOOK, json={"ok": True, "result": True}, status_code=200)


@pytest.fixture
def _enabled_with_env_token(monkeypatch):
    """An env-locked token, which is the shape that never reaches the form's ``clean``."""
    from core.site_settings import _docker_secret_cache, site_settings

    monkeypatch.setenv("DAIV_TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("DAIV_TELEGRAM_ENABLED", "true")
    for key in ("DAIV_TELEGRAM_BOT_TOKEN", "DAIV_TELEGRAM_ENABLED"):
        _docker_secret_cache.pop(key, None)
    SiteConfiguration._invalidate_cache()
    try:
        yield site_settings
    finally:
        for key in ("DAIV_TELEGRAM_BOT_TOKEN", "DAIV_TELEGRAM_ENABLED"):
            _docker_secret_cache.pop(key, None)
        SiteConfiguration._invalidate_cache()


class TestWebhookUrl:
    def test_is_built_from_the_sites_framework(self):
        assert webhook_url().endswith("/api/notifications/callbacks/telegram/")
        assert webhook_url().startswith("https://")


@pytest.mark.usefixtures("_enabled_with_env_token")
class TestSyncFromAnEnvLockedToken:
    def test_derives_the_username_and_registers_the_webhook(self, httpx_mock):
        _ok_get_me(httpx_mock)
        _ok_set_webhook(httpx_mock)
        assert sync_telegram() == []
        config = SiteConfiguration.objects.get(pk=1)
        assert config.telegram_bot_username == "daiv_bot"
        assert config.telegram_webhook_secret

    def test_generates_the_webhook_secret_exactly_once(self, httpx_mock):
        _ok_get_me(httpx_mock)
        _ok_set_webhook(httpx_mock)
        sync_telegram()
        first = SiteConfiguration.objects.get(pk=1).telegram_webhook_secret
        SiteConfiguration._invalidate_cache()
        _ok_get_me(httpx_mock)
        _ok_set_webhook(httpx_mock)
        sync_telegram()
        assert SiteConfiguration.objects.get(pk=1).telegram_webhook_secret == first

    def test_the_secret_reaches_set_webhook(self, httpx_mock):
        import json

        _ok_get_me(httpx_mock)
        _ok_set_webhook(httpx_mock)
        sync_telegram()
        secret = SiteConfiguration.objects.get(pk=1).telegram_webhook_secret
        body = json.loads(next(r for r in httpx_mock.get_requests() if str(r.url) == SET_WEBHOOK).content)
        assert body["secret_token"] == secret
        assert body["url"] == webhook_url()

    def test_the_username_is_re_derived_so_a_rotated_token_self_heals(self, httpx_mock):
        _ok_get_me(httpx_mock, username="first_bot")
        _ok_set_webhook(httpx_mock)
        sync_telegram()
        SiteConfiguration._invalidate_cache()
        _ok_get_me(httpx_mock, username="second_bot")
        _ok_set_webhook(httpx_mock)
        sync_telegram()
        assert SiteConfiguration.objects.get(pk=1).telegram_bot_username == "second_bot"

    def test_a_get_me_transport_failure_warns_and_leaves_the_save_committed(self, httpx_mock):
        import httpx

        httpx_mock.add_exception(httpx.ConnectError("no route"), url=GET_ME)
        _ok_set_webhook(httpx_mock)
        warnings = sync_telegram()
        assert warnings and "bot username" in warnings[0]
        # The secret still lands, so the fail-closed route is not left rejecting forever.
        assert SiteConfiguration.objects.get(pk=1).telegram_webhook_secret

    def test_a_set_webhook_failure_warns_without_raising(self, httpx_mock):
        _ok_get_me(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=SET_WEBHOOK,
            json={"ok": False, "error_code": 400, "description": "bad url"},
            status_code=400,
        )
        warnings = sync_telegram()
        assert warnings and "webhook" in warnings[0].lower()


class TestSyncWhenDisabled:
    def test_disabling_the_toggle_deletes_the_webhook(self, httpx_mock, site_settings_override):
        from pydantic import SecretStr

        site_settings_override(telegram_enabled=False, telegram_bot_token=SecretStr("123:ABC"))
        httpx_mock.add_response(method="POST", url=DELETE_WEBHOOK, json={"ok": True, "result": True}, status_code=200)
        assert sync_telegram() == []
        assert [str(r.url) for r in httpx_mock.get_requests()] == [DELETE_WEBHOOK]

    def test_disabling_keeps_the_stored_secret(self, httpx_mock, site_settings_override):
        # The webhook route answers 2xx while the channel is off ONLY while the stored secret
        # still matches; clearing it here would turn disabled-channel updates into 401s.
        from pydantic import SecretStr

        config = SiteConfiguration.objects.get_instance()
        config.telegram_webhook_secret = "kept"  # noqa: S105 — test constant
        config.save()
        site_settings_override(telegram_enabled=False, telegram_bot_token=SecretStr("123:ABC"))
        httpx_mock.add_response(method="POST", url=DELETE_WEBHOOK, json={"ok": True, "result": True}, status_code=200)
        sync_telegram()
        assert SiteConfiguration.objects.get(pk=1).telegram_webhook_secret == "kept"  # noqa: S105 — test constant

    def test_enabled_without_a_token_warns_and_calls_nothing(self, httpx_mock, site_settings_override):
        site_settings_override(telegram_enabled=True, telegram_bot_token=None)
        warnings = sync_telegram()
        assert warnings and "token" in warnings[0].lower()
        assert httpx_mock.get_requests() == []


@pytest.mark.django_db
class TestConfigurationSaveHook:
    URL = "/dashboard/configuration/telegram/"

    def test_saving_the_group_derives_the_username(self, admin_client, httpx_mock):
        # Two getMe calls: one in clean() for blocking feedback, one in the hook for the write.
        _ok_get_me(httpx_mock)
        _ok_get_me(httpx_mock)
        _ok_set_webhook(httpx_mock)
        response = admin_client.post(self.URL, {"telegram_enabled": "on", "telegram_bot_token": "123:ABC"})
        assert response.status_code == 302
        assert SiteConfiguration.objects.get(pk=1).telegram_bot_username == "daiv_bot"

    def test_a_rejected_token_is_a_validation_error_not_a_warning(self, admin_client, httpx_mock):
        # The URL carries the *typed* token, so a rejected-token test cannot reuse GET_ME.
        httpx_mock.add_response(
            method="POST",
            url="https://api.telegram.org/botbad-token/getMe",
            json={"ok": False, "error_code": 401, "description": "Unauthorized"},
            status_code=401,
        )
        response = admin_client.post(self.URL, {"telegram_enabled": "on", "telegram_bot_token": "bad-token"})
        assert response.status_code == 200  # re-rendered with errors
        assert "rejected this bot token" in response.content.decode()
        assert SiteConfiguration.objects.get(pk=1).telegram_bot_token is None

    def test_a_telegram_outage_lets_the_save_through(self, admin_client, httpx_mock):
        import httpx

        httpx_mock.add_exception(httpx.ConnectError("no route"), url=GET_ME, is_reusable=True)
        # The hook still reaches setWebhook after the failed getMe, and an unregistered request
        # would fail this test at teardown rather than exercising the outage path.
        _ok_set_webhook(httpx_mock)
        response = admin_client.post(self.URL, {"telegram_enabled": "on", "telegram_bot_token": "123:ABC"})
        assert response.status_code == 302
        assert SiteConfiguration.objects.get(pk=1).telegram_bot_token == "123:ABC"  # noqa: S105 — test constant

    @pytest.mark.usefixtures("_enabled_with_env_token")
    def test_an_env_locked_token_is_never_validated_through_the_form(self, admin_client, httpx_mock):
        # The field is disabled, so Django cleans its *initial* value: ``str(SecretStr)``, i.e.
        # the mask. Validating that would 401 and make the group unsavable on such a deployment.
        _ok_get_me(httpx_mock)
        _ok_set_webhook(httpx_mock)
        response = admin_client.post(self.URL, {"telegram_enabled": "on"})
        assert response.status_code == 302
        assert [str(r.url) for r in httpx_mock.get_requests()] == [GET_ME, SET_WEBHOOK]
        assert SiteConfiguration.objects.get(pk=1).telegram_bot_username == "daiv_bot"

    def test_saving_a_different_group_never_calls_telegram(self, admin_client, httpx_mock):
        admin_client.post("/dashboard/configuration/jobs/", {"jobs_throttle_rate": "10/hour"})
        assert httpx_mock.get_requests() == []
