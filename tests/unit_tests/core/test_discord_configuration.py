from __future__ import annotations

import pytest

from core.models import SiteConfiguration
from core.site_settings import site_settings


@pytest.mark.django_db
class TestDiscordFieldGroup:
    def test_group_exists_with_the_toggle_and_integrations_category(self):
        group = SiteConfiguration.get_group_by_key("discord")
        assert group.toggle_field == "discord_enabled"
        assert group.category == "Integrations"
        assert group.icon == "discord"
        assert set(group.fields) == {"discord_enabled", "discord_bot_token"}

    def test_bot_token_is_encrypted_at_rest(self):
        assert "discord_bot_token" in SiteConfiguration.ENCRYPTED_FIELDS
        config = SiteConfiguration.objects.get_instance()
        config.discord_bot_token = "discord-secret"  # noqa: S105
        config.save()
        reloaded = SiteConfiguration.objects.get(pk=1)
        assert reloaded.discord_bot_token == "discord-secret"  # noqa: S105
        assert reloaded._discord_bot_token_encrypted != "discord-secret"  # noqa: S105


@pytest.mark.django_db
class TestDiscordSiteSettings:
    def test_disabled_by_default(self):
        assert site_settings.discord_enabled is False

    def test_db_value_resolves_as_a_secret_str(self):
        config = SiteConfiguration.objects.get_instance()
        config.discord_bot_token = "discord-secret"  # noqa: S105
        config.save()
        SiteConfiguration._invalidate_cache()
        try:
            assert site_settings.discord_bot_token.get_secret_value() == "discord-secret"
        finally:
            SiteConfiguration._invalidate_cache()

    def test_env_var_overrides_the_db(self, monkeypatch):
        monkeypatch.setenv("DAIV_DISCORD_BOT_TOKEN", "env-token")
        assert site_settings.discord_bot_token.get_secret_value() == "env-token"
        assert site_settings.is_env_locked("discord_bot_token") is True
