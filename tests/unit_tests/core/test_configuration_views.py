from django.urls import reverse

import pytest

from accounts.models import Role, User
from core.models import SiteConfiguration


@pytest.fixture
def admin_user(db):
    return User.objects.create_user(
        username="admin",
        email="admin@test.com",
        password="testpass123",  # noqa: S106
        role=Role.ADMIN,
    )


@pytest.fixture
def member_user(db):
    return User.objects.create_user(
        username="member",
        email="member@test.com",
        password="testpass123",  # noqa: S106
        role=Role.MEMBER,
    )


@pytest.fixture
def url():
    return reverse("site_configuration")


class TestGetAccess:
    def test_admin_can_access(self, client, admin_user, url):
        client.force_login(admin_user)
        response = client.get(url)
        assert response.status_code == 200

    def test_member_cannot_access(self, client, member_user, url):
        client.force_login(member_user)
        response = client.get(url)
        assert response.status_code == 403

    def test_anonymous_redirected(self, client, url):
        response = client.get(url)
        assert response.status_code == 302


class TestGetContent:
    def test_renders_form(self, client, admin_user, url):
        client.force_login(admin_user)
        response = client.get(url)
        assert b"Configuration" in response.content
        assert b"Agent" in response.content
        assert b"Save configuration" in response.content

    def test_renders_field_groups(self, client, admin_user, url):
        client.force_login(admin_user)
        response = client.get(url)
        content = response.content.decode()
        assert "Web Search" in content
        assert "Web Fetch" in content
        assert "Sandbox" in content
        assert "Providers" in content
        assert "Jobs" in content

    def test_renders_default_placeholders(self, client, admin_user, url):
        client.force_login(admin_user)
        response = client.get(url)
        content = response.content.decode()
        assert 'placeholder="500"' in content
        assert 'placeholder="20/hour"' in content
        assert "Default (" in content


class TestBooleanCheckboxField:
    def test_checked_returns_true(self):
        from core.forms import _BooleanCheckboxField

        field = _BooleanCheckboxField()
        assert field.clean("on") is True

    def test_unchecked_returns_false(self):
        from core.forms import _BooleanCheckboxField

        field = _BooleanCheckboxField()
        assert field.clean("") is False

    def test_missing_returns_false(self):
        from core.forms import _BooleanCheckboxField

        field = _BooleanCheckboxField()
        assert field.clean(None) is False


class TestPostSave:
    def test_save_plain_settings(self, client, admin_user, url):
        config = SiteConfiguration.objects.get_instance()
        config.anthropic_api_key = "sk-test"
        config.save()

        client.force_login(admin_user)
        response = client.post(
            url,
            {
                "agent_model_name_provider": "anthropic",
                "agent_model_name_model": "claude-sonnet-4-6",
                "agent_recursion_limit": 100,
            },
        )
        assert response.status_code == 302

        config.refresh_from_db()
        assert config.agent_model_name == "anthropic:claude-sonnet-4-6"
        assert config.agent_recursion_limit == 100

    def test_save_boolean_checked(self, client, admin_user, url):
        client.force_login(admin_user)
        response = client.post(url, {"web_search_enabled": "on"})
        assert response.status_code == 302

        config = SiteConfiguration.objects.get_instance()
        assert config.web_search_enabled is True

    def test_save_boolean_unchecked(self, client, admin_user, url):
        """Unchecked checkbox should save False, not None."""
        config = SiteConfiguration.objects.get_instance()
        config.web_search_enabled = True
        config.save()

        client.force_login(admin_user)
        # POST without web_search_enabled = checkbox unchecked
        response = client.post(url, {})
        assert response.status_code == 302

        config.refresh_from_db()
        assert config.web_search_enabled is False

    def test_empty_secret_preserves_existing(self, client, admin_user, url, db):
        config = SiteConfiguration.objects.get_instance()
        config.anthropic_api_key = "sk-existing-key"
        config.save()

        client.force_login(admin_user)
        response = client.post(
            url, {"agent_model_name_provider": "anthropic", "agent_model_name_model": "claude-sonnet-4-6"}
        )
        assert response.status_code == 302

        config.refresh_from_db()
        assert config.anthropic_api_key == "sk-existing-key"

    def test_clear_secret_via_model(self, db):
        config = SiteConfiguration.objects.get_instance()
        config.anthropic_api_key = "sk-existing-key"
        config.save()

        config._anthropic_api_key_encrypted = None
        config.save()

        row = SiteConfiguration.objects.values("_anthropic_api_key_encrypted").get(pk=1)
        assert row["_anthropic_api_key_encrypted"] is None

    def test_form_clear_secret_directly(self, db):
        config = SiteConfiguration.objects.get_instance()
        config.anthropic_api_key = "sk-existing-key"
        config.save()

        from core.forms import SiteConfigurationForm

        form = SiteConfigurationForm(data={}, instance=config, cleared_secrets={"anthropic_api_key"})
        assert form.is_valid(), f"Form errors: {form.errors}"
        form.save()

        row = SiteConfiguration.objects.values("_anthropic_api_key_encrypted").get(pk=1)
        assert row["_anthropic_api_key_encrypted"] is None

    def test_clear_secret(self, client, admin_user, url, db):
        from core.forms import SiteConfigurationForm

        config = SiteConfiguration.objects.get_instance()
        config.anthropic_api_key = "sk-existing-key"
        config.save()

        config = SiteConfiguration.objects.get_instance()
        cleared = {"anthropic_api_key"}
        form = SiteConfigurationForm(data={}, instance=config, cleared_secrets=cleared)
        assert form.is_valid()
        form.save()

        row = SiteConfiguration.objects.values("_anthropic_api_key_encrypted").get(pk=1)
        assert row["_anthropic_api_key_encrypted"] is None

    def test_member_cannot_post(self, client, member_user, url):
        client.force_login(member_user)
        response = client.post(
            url, {"agent_model_name_provider": "anthropic", "agent_model_name_model": "claude-sonnet-4-6"}
        )
        assert response.status_code == 403


class TestModelApiKeyValidation:
    def test_model_without_api_key_rejected(self, db):
        from core.forms import SiteConfigurationForm

        config = SiteConfiguration.objects.get_instance()
        form = SiteConfigurationForm(
            data={"agent_model_name_provider": "anthropic", "agent_model_name_model": "claude-sonnet-4-6"},
            instance=config,
        )
        assert not form.is_valid()
        assert "agent_model_name" in form.errors

    def test_model_with_api_key_in_form_accepted(self, db):
        from core.forms import SiteConfigurationForm

        config = SiteConfiguration.objects.get_instance()
        form = SiteConfigurationForm(
            data={
                "agent_model_name_provider": "anthropic",
                "agent_model_name_model": "claude-sonnet-4-6",
                "anthropic_api_key": "sk-test",
            },
            instance=config,
        )
        assert form.is_valid(), f"Form errors: {form.errors}"

    def test_model_with_api_key_in_db_accepted(self, db):
        from core.forms import SiteConfigurationForm

        config = SiteConfiguration.objects.get_instance()
        config.anthropic_api_key = "sk-existing"
        config.save()

        form = SiteConfigurationForm(
            data={"agent_model_name_provider": "anthropic", "agent_model_name_model": "claude-sonnet-4-6"},
            instance=config,
        )
        assert form.is_valid(), f"Form errors: {form.errors}"

    def test_model_with_api_key_env_locked_accepted(self, db, monkeypatch):
        from core.forms import SiteConfigurationForm

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
        config = SiteConfiguration.objects.get_instance()
        form = SiteConfigurationForm(
            data={"agent_model_name_provider": "anthropic", "agent_model_name_model": "claude-sonnet-4-6"},
            instance=config,
            env_locked_fields={"anthropic_api_key"},
        )
        assert form.is_valid(), f"Form errors: {form.errors}"

    def test_clearing_api_key_while_model_set_rejected(self, db):
        from core.forms import SiteConfigurationForm

        config = SiteConfiguration.objects.get_instance()
        config.anthropic_api_key = "sk-existing"
        config.save()

        form = SiteConfigurationForm(
            data={"agent_model_name_provider": "anthropic", "agent_model_name_model": "claude-sonnet-4-6"},
            instance=config,
            cleared_secrets={"anthropic_api_key"},
        )
        assert not form.is_valid()
        assert "agent_model_name" in form.errors

    def test_unknown_model_with_default_provider_needs_api_key(self, db):
        """With the composite widget, provider is always resolved. An unknown model
        name with the default provider (openrouter) is valid but requires the API key."""
        from core.forms import SiteConfigurationForm

        config = SiteConfiguration.objects.get_instance()
        form = SiteConfigurationForm(
            data={"agent_model_name_provider": "", "agent_model_name_model": "unknown-model-xyz"}, instance=config
        )
        assert not form.is_valid()
        assert "agent_model_name" in form.errors
        assert "api key" in str(form.errors["agent_model_name"]).lower()

    def test_empty_model_name_skips_validation(self, db):
        from core.forms import SiteConfigurationForm

        config = SiteConfiguration.objects.get_instance()
        form = SiteConfigurationForm(data={}, instance=config)
        assert form.is_valid(), f"Form errors: {form.errors}"

    def test_multiple_models_same_provider_one_error(self, db):
        from core.forms import SiteConfigurationForm

        config = SiteConfiguration.objects.get_instance()
        form = SiteConfigurationForm(
            data={
                "agent_model_name_provider": "anthropic",
                "agent_model_name_model": "claude-sonnet-4-6",
                "agent_fallback_model_name_provider": "anthropic",
                "agent_fallback_model_name_model": "claude-haiku-4-5",
            },
            instance=config,
        )
        assert not form.is_valid()
        assert "agent_model_name" in form.errors
        assert "agent_fallback_model_name" in form.errors

    def test_openai_model_requires_openai_key(self, db):
        from core.forms import SiteConfigurationForm

        config = SiteConfiguration.objects.get_instance()
        form = SiteConfigurationForm(
            data={
                "diff_to_metadata_model_name_provider": "openai",
                "diff_to_metadata_model_name_model": "gpt-5.4-mini",
            },
            instance=config,
        )
        assert not form.is_valid()
        assert "diff_to_metadata_model_name" in form.errors

    def test_openrouter_model_requires_openrouter_key(self, db):
        from core.forms import SiteConfigurationForm

        config = SiteConfiguration.objects.get_instance()
        form = SiteConfigurationForm(
            data={
                "agent_model_name_provider": "openrouter",
                "agent_model_name_model": "some-model",
                "openrouter_api_key": "or-key",
            },
            instance=config,
        )
        assert form.is_valid(), f"Form errors: {form.errors}"


class TestWebSearchApiKeyValidation:
    def test_tavily_without_api_key_rejected(self, db):
        from core.forms import SiteConfigurationForm

        config = SiteConfiguration.objects.get_instance()
        form = SiteConfigurationForm(data={"web_search_engine": "tavily"}, instance=config)
        assert not form.is_valid()
        assert "web_search_engine" in form.errors

    def test_tavily_with_api_key_accepted(self, db):
        from core.forms import SiteConfigurationForm

        config = SiteConfiguration.objects.get_instance()
        config.web_search_api_key = "tvly-test"
        config.save()

        form = SiteConfigurationForm(data={"web_search_engine": "tavily"}, instance=config)
        assert form.is_valid(), f"Form errors: {form.errors}"

    def test_duckduckgo_without_api_key_accepted(self, db):
        from core.forms import SiteConfigurationForm

        config = SiteConfiguration.objects.get_instance()
        form = SiteConfigurationForm(data={"web_search_engine": "duckduckgo"}, instance=config)
        assert form.is_valid(), f"Form errors: {form.errors}"


class TestClearSecretViaHttp:
    """Test gap #10: clear_secret POST key convention exercised through HTTP client."""

    def test_clear_secret_via_post(self, client, admin_user, url, db, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        config = SiteConfiguration.objects.get_instance()
        config.anthropic_api_key = "sk-existing-key"
        config.save()

        client.force_login(admin_user)
        response = client.post(url, {"clear_anthropic_api_key": "1"})
        assert response.status_code == 302

        config.refresh_from_db()
        assert config.anthropic_api_key is None
        assert config._anthropic_api_key_encrypted is None


class TestEnvLockedSecretSave:
    """Test gap #12: env-locked secrets are not overwritten by form submission."""

    def test_env_locked_secret_not_overwritten(self, db):
        from core.forms import SiteConfigurationForm

        config = SiteConfiguration.objects.get_instance()
        config.anthropic_api_key = "sk-original"
        config.save()

        form = SiteConfigurationForm(
            data={"anthropic_api_key": "sk-new-value"}, instance=config, env_locked_fields={"anthropic_api_key"}
        )
        assert form.is_valid(), f"Form errors: {form.errors}"
        form.save()

        config.refresh_from_db()
        assert config.anthropic_api_key == "sk-original"
