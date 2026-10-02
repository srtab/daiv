from __future__ import annotations

import pytest

from core.models import SiteConfiguration
from core.site_settings import site_settings


@pytest.mark.django_db
class TestTelegramFieldGroup:
    def test_group_exists_with_the_toggle_and_the_integrations_category(self):
        group = SiteConfiguration.get_group_by_key("telegram")
        assert group.toggle_field == "telegram_enabled"
        assert group.category == "Integrations"
        assert group.icon == "telegram"

    def test_group_owns_all_four_telegram_fields(self):
        group = SiteConfiguration.get_group_by_key("telegram")
        assert set(group.fields) == {
            "telegram_enabled",
            "telegram_bot_username",
            "telegram_bot_token",
            "telegram_webhook_secret",
        }

    def test_both_secrets_are_encrypted_at_rest(self):
        assert "telegram_bot_token" in SiteConfiguration.ENCRYPTED_FIELDS
        assert "telegram_webhook_secret" in SiteConfiguration.ENCRYPTED_FIELDS


@pytest.mark.django_db
class TestTelegramSecretDescriptors:
    def test_bot_token_round_trips_through_the_encrypted_column(self):
        config = SiteConfiguration.objects.get_instance()
        config.telegram_bot_token = "123:ABC"  # noqa: S105
        config.save()
        reloaded = SiteConfiguration.objects.get(pk=1)
        assert reloaded.telegram_bot_token == "123:ABC"  # noqa: S105
        assert reloaded._telegram_bot_token_encrypted != "123:ABC"  # noqa: S105

    def test_webhook_secret_round_trips_and_clears(self):
        config = SiteConfiguration.objects.get_instance()
        config.telegram_webhook_secret = "wh-secret"  # noqa: S105
        config.save()
        assert SiteConfiguration.objects.get(pk=1).telegram_webhook_secret == "wh-secret"  # noqa: S105
        config.telegram_webhook_secret = None
        config.save()
        assert SiteConfiguration.objects.get(pk=1).telegram_webhook_secret is None


@pytest.mark.django_db
class TestSiteSettingsResolution:
    def test_disabled_by_default_with_no_derived_username(self):
        assert site_settings.telegram_enabled is False
        assert site_settings.telegram_bot_username is None

    def test_db_value_resolves_as_a_secret_str(self):
        config = SiteConfiguration.objects.get_instance()
        config.telegram_bot_token = "123:ABC"  # noqa: S105
        config.save()
        SiteConfiguration._invalidate_cache()
        try:
            assert site_settings.telegram_bot_token.get_secret_value() == "123:ABC"
        finally:
            SiteConfiguration._invalidate_cache()

    def test_env_var_overrides_the_db(self, monkeypatch):
        # ``_clear_docker_secret_cache`` (autouse, the root conftest) empties the cache
        # around every test, so the env var is read fresh here.
        monkeypatch.setenv("DAIV_TELEGRAM_BOT_TOKEN", "env:TOKEN")
        assert site_settings.telegram_bot_token.get_secret_value() == "env:TOKEN"
        assert site_settings.is_env_locked("telegram_bot_token") is True
