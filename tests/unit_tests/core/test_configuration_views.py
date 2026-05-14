from unittest.mock import patch

from django.urls import reverse

import pytest

from accounts.models import Role, User
from core.models import Provider, SiteConfiguration


def _enable_seed_provider(slug: str, api_key: str = "sk-test") -> Provider:
    provider = Provider.objects.get(slug=slug)
    provider.api_key = api_key
    provider.is_enabled = True
    provider.save()
    return provider


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
    # Default landing URL for the agent group (used by tests that don't care which group).
    return reverse("site_configuration", kwargs={"group_key": "agent"})


@pytest.fixture
def group_url():
    def _build(group_key: str) -> str:
        return reverse("site_configuration", kwargs={"group_key": group_key})

    return _build


@pytest.fixture
def index_url():
    return reverse("site_configuration_index")


def _providers_mgmt(seed_count: int = 4) -> dict[str, str]:
    """Management form for the providers formset. Submitting unchanged rows requires sending all initial forms."""
    fields: dict[str, str] = {
        "providers-TOTAL_FORMS": str(seed_count),
        "providers-INITIAL_FORMS": str(seed_count),
        "providers-MIN_NUM_FORMS": "0",
        "providers-MAX_NUM_FORMS": "1000",
    }
    for idx, p in enumerate(Provider.objects.order_by("sort_order", "slug")):
        fields[f"providers-{idx}-id"] = str(p.pk)
        fields[f"providers-{idx}-slug"] = p.slug
        fields[f"providers-{idx}-display_name"] = p.display_name
        fields[f"providers-{idx}-provider_type"] = p.provider_type
        fields[f"providers-{idx}-base_url"] = p.base_url
        fields[f"providers-{idx}-extra_headers"] = "{}"
        fields[f"providers-{idx}-model_suggestions"] = p.model_suggestions
        if p.is_enabled:
            fields[f"providers-{idx}-is_enabled"] = "on"
        fields[f"providers-{idx}-sort_order"] = str(p.sort_order)
    return fields


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

    def test_renders_agent_placeholders(self, client, admin_user, url):
        client.force_login(admin_user)
        response = client.get(url)
        content = response.content.decode()
        assert 'placeholder="500"' in content  # agent_recursion_limit default
        assert "Default (" in content

    def test_renders_jobs_placeholder(self, client, admin_user, group_url):
        client.force_login(admin_user)
        response = client.get(group_url("jobs"))
        content = response.content.decode()
        assert 'placeholder="20/hour"' in content


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
    def test_save_plain_settings(self, client, admin_user, group_url):
        _enable_seed_provider("anthropic")

        client.force_login(admin_user)
        response = client.post(
            group_url("agent"),
            {
                "agent_model_name_provider": "anthropic",
                "agent_model_name_model": "claude-sonnet-4-6",
                "agent_recursion_limit": 100,
            },
        )
        assert response.status_code == 302

        config = SiteConfiguration.objects.get_instance()
        assert config.agent_model_name == "anthropic:claude-sonnet-4-6"
        assert config.agent_recursion_limit == 100

    def test_save_boolean_checked(self, client, admin_user, group_url):
        client.force_login(admin_user)
        response = client.post(group_url("web_search"), {"web_search_enabled": "on"})
        assert response.status_code == 302

        config = SiteConfiguration.objects.get_instance()
        assert config.web_search_enabled is True

    def test_save_boolean_unchecked(self, client, admin_user, group_url):
        """Unchecked checkbox should save False, not None."""
        config = SiteConfiguration.objects.get_instance()
        config.web_search_enabled = True
        config.save()

        client.force_login(admin_user)
        # POST without web_search_enabled = checkbox unchecked
        response = client.post(group_url("web_search"), {})
        assert response.status_code == 302

        config.refresh_from_db()
        assert config.web_search_enabled is False

    def test_member_cannot_post(self, client, member_user, group_url):
        client.force_login(member_user)
        response = client.post(
            group_url("agent"),
            {"agent_model_name_provider": "anthropic", "agent_model_name_model": "claude-sonnet-4-6"},
        )
        assert response.status_code == 403

    def test_error_banner_renders_when_slug_invalid(self, client, admin_user, group_url):
        """Regression: row with invalid slug drops slug from cleaned_data; banner must
        still render without VariableDoesNotExist. Also assert no partial commit."""
        client.force_login(admin_user)
        seed_count = Provider.objects.count()
        mgmt = _providers_mgmt()
        new_idx = int(mgmt["providers-TOTAL_FORMS"])
        mgmt["providers-TOTAL_FORMS"] = str(new_idx + 1)
        # Submit a slug that fails clean_slug (starts with digit, not a letter).
        bad_row = {
            f"providers-{new_idx}-id": "",
            f"providers-{new_idx}-slug": "9bad",
            f"providers-{new_idx}-display_name": "Bad",
            f"providers-{new_idx}-provider_type": "openrouter",
            f"providers-{new_idx}-base_url": "",
            f"providers-{new_idx}-api_key": "sk-x",
            f"providers-{new_idx}-extra_headers": "{}",
            f"providers-{new_idx}-model_suggestions": "",
            f"providers-{new_idx}-sort_order": "0",
        }
        response = client.post(group_url("providers"), {**mgmt, **bad_row})
        assert response.status_code == 200
        content = response.content.decode()
        assert "Providers could not be saved" in content
        assert "Slug must start with a lowercase letter" in content
        assert Provider.objects.count() == seed_count

    def test_renders_sort_order_hidden_input(self, client, admin_user, group_url):
        """Row template must emit sort_order as a hidden input so the browser submits it.

        Regression: without this, every row fails validation with "sort_order: This field
        is required." because the field isn't rendered visibly anywhere.
        """
        client.force_login(admin_user)
        response = client.get(group_url("providers"))
        content = response.content.decode()
        for idx in range(Provider.objects.count()):
            assert f'type="hidden" name="providers-{idx}-sort_order"' in content, (
                f"row {idx} missing sort_order hidden input"
            )

    def test_clear_api_key_via_button(self, client, admin_user, group_url):
        """clear_api_key=on forces api_key=None and is_enabled=False even when the
        POST also re-sends is_enabled=on for that row — disable wins."""
        provider = _enable_seed_provider("anthropic", api_key="sk-existing")
        client.force_login(admin_user)
        mgmt = _providers_mgmt()
        anthropic_idx = next(
            idx for idx, p in enumerate(Provider.objects.order_by("sort_order", "slug")) if p.pk == provider.pk
        )
        # is_enabled=on is already in mgmt (anthropic was enabled); add clear_api_key=on too.
        assert mgmt[f"providers-{anthropic_idx}-is_enabled"] == "on"
        response = client.post(group_url("providers"), {**mgmt, f"providers-{anthropic_idx}-clear_api_key": "on"})
        assert response.status_code == 302

        provider.refresh_from_db()
        assert provider.api_key is None
        assert provider.is_enabled is False

    def test_add_new_row_preserves_existing_enabled_rows(self, client, admin_user, group_url):
        """Regression: adding a new provider row must not reset api_key/is_enabled on existing rows."""
        openai = _enable_seed_provider("openai", api_key="sk-openai-key")
        anthropic = _enable_seed_provider("anthropic", api_key="sk-anthropic-key")

        client.force_login(admin_user)
        mgmt = _providers_mgmt()
        new_idx = int(mgmt["providers-TOTAL_FORMS"])
        mgmt["providers-TOTAL_FORMS"] = str(new_idx + 1)
        new_row = {
            f"providers-{new_idx}-id": "",
            f"providers-{new_idx}-slug": "my-custom",
            f"providers-{new_idx}-display_name": "My Custom",
            f"providers-{new_idx}-provider_type": "openrouter",
            f"providers-{new_idx}-base_url": "https://example.com/v1",
            f"providers-{new_idx}-api_key": "sk-new-key",
            f"providers-{new_idx}-extra_headers": "{}",
            f"providers-{new_idx}-model_suggestions": "",
            f"providers-{new_idx}-is_enabled": "on",
            f"providers-{new_idx}-sort_order": "100",
        }
        response = client.post(group_url("providers"), {**mgmt, **new_row})
        assert response.status_code == 302

        openai.refresh_from_db()
        anthropic.refresh_from_db()
        assert openai.api_key == "sk-openai-key"
        assert openai.is_enabled is True
        assert anthropic.api_key == "sk-anthropic-key"
        assert anthropic.is_enabled is True

    def test_clear_api_key_on_locked_row(self, client, admin_user, group_url):
        """Locked seed rows can have their api_key cleared — is_locked only guards slug/type/delete."""
        provider = _enable_seed_provider("openai", api_key="sk-openai-key")
        assert provider.is_locked is True

        client.force_login(admin_user)
        mgmt = _providers_mgmt()
        idx = next(i for i, p in enumerate(Provider.objects.order_by("sort_order", "slug")) if p.pk == provider.pk)
        response = client.post(group_url("providers"), {**mgmt, f"providers-{idx}-clear_api_key": "on"})
        assert response.status_code == 302

        provider.refresh_from_db()
        assert provider.api_key is None
        assert provider.is_enabled is False
        assert provider.is_locked is True

    def test_add_new_provider_row(self, client, admin_user, group_url):
        """A JS-added row (index past the seed count, no id) creates a new Provider."""
        client.force_login(admin_user)
        mgmt = _providers_mgmt()
        new_idx = int(mgmt["providers-TOTAL_FORMS"])
        mgmt["providers-TOTAL_FORMS"] = str(new_idx + 1)
        new_row = {
            f"providers-{new_idx}-id": "",
            f"providers-{new_idx}-slug": "custom-router",
            f"providers-{new_idx}-display_name": "Custom Router",
            f"providers-{new_idx}-provider_type": "openrouter",
            f"providers-{new_idx}-base_url": "https://example.test/v1",
            f"providers-{new_idx}-api_key": "sk-custom",
            f"providers-{new_idx}-extra_headers": "{}",
            f"providers-{new_idx}-model_suggestions": "",
            f"providers-{new_idx}-is_enabled": "on",
            f"providers-{new_idx}-sort_order": "100",
        }
        response = client.post(group_url("providers"), {**mgmt, **new_row})
        assert response.status_code == 302

        created = Provider.objects.get(slug="custom-router")
        assert created.provider_type == "openrouter"
        assert created.is_locked is False
        assert created.is_enabled is True
        assert created.api_key == "sk-custom"


class TestModelApiKeyValidation:
    def test_model_without_enabled_provider_rejected(self, db):
        """Default seed rows are disabled when no env var is set; selecting one should fail."""
        from core.forms import SiteConfigurationForm

        # Ensure the anthropic seed row is disabled.
        p = Provider.objects.get(slug="anthropic")
        p.api_key = None
        p.is_enabled = True
        p.save()

        config = SiteConfiguration.objects.get_instance()
        form = SiteConfigurationForm(
            data={"agent_model_name_provider": "anthropic", "agent_model_name_model": "claude-sonnet-4-6"},
            instance=config,
        )
        assert not form.is_valid()
        assert "agent_model_name" in form.errors
        assert "API key" in str(form.errors["agent_model_name"][0])

    def test_disabled_provider_rejected(self, db):
        from core.forms import SiteConfigurationForm

        p = Provider.objects.get(slug="anthropic")
        p.api_key = "sk-x"
        p.is_enabled = False
        p.save()

        config = SiteConfiguration.objects.get_instance()
        form = SiteConfigurationForm(
            data={"agent_model_name_provider": "anthropic", "agent_model_name_model": "claude-sonnet-4-6"},
            instance=config,
        )
        assert not form.is_valid()
        assert "agent_model_name" in form.errors
        assert "disabled" in str(form.errors["agent_model_name"][0])

    def test_enabled_provider_with_key_accepted(self, db):
        from core.forms import SiteConfigurationForm

        _enable_seed_provider("anthropic")

        config = SiteConfiguration.objects.get_instance()
        form = SiteConfigurationForm(
            data={"agent_model_name_provider": "anthropic", "agent_model_name_model": "claude-sonnet-4-6"},
            instance=config,
        )
        assert form.is_valid(), f"Form errors: {form.errors}"

    def test_empty_model_name_skips_validation(self, db):
        from core.forms import SiteConfigurationForm

        config = SiteConfiguration.objects.get_instance()
        form = SiteConfigurationForm(data={}, instance=config)
        assert form.is_valid(), f"Form errors: {form.errors}"

    def test_multiple_unconfigured_models_each_get_errors(self, db):
        from core.forms import SiteConfigurationForm

        p = Provider.objects.get(slug="anthropic")
        p.api_key = None
        p.is_enabled = True
        p.save()

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

    def test_unknown_provider_prefix_rejected(self, db):
        from core.forms import SiteConfigurationForm

        config = SiteConfiguration.objects.get_instance()
        form = SiteConfigurationForm(
            data={"agent_model_name_provider": "no-such-provider", "agent_model_name_model": "some-model"},
            instance=config,
        )
        assert not form.is_valid()
        assert "agent_model_name" in form.errors


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
    """clear_secret POST key convention exercised through HTTP client."""

    def test_clear_secret_via_post(self, client, admin_user, group_url, db, monkeypatch):
        monkeypatch.delenv("DAIV_WEB_SEARCH_API_KEY", raising=False)

        config = SiteConfiguration.objects.get_instance()
        config.web_search_api_key = "sk-existing-key"
        config.save()

        client.force_login(admin_user)
        response = client.post(group_url("web_search"), {"clear_web_search_api_key": "1"})
        assert response.status_code == 302

        config.refresh_from_db()
        assert config.web_search_api_key is None
        assert config._web_search_api_key_encrypted is None


class TestEnvLockedSecretSave:
    """Env-locked secrets are not overwritten by form submission."""

    def test_env_locked_secret_not_overwritten(self, db):
        from core.forms import SiteConfigurationForm

        config = SiteConfiguration.objects.get_instance()
        config.web_search_api_key = "sk-original"
        config.save()

        form = SiteConfigurationForm(
            data={"web_search_api_key": "sk-new-value"}, instance=config, env_locked_fields={"web_search_api_key"}
        )
        assert form.is_valid(), f"Form errors: {form.errors}"
        form.save()

        config.refresh_from_db()
        assert config.web_search_api_key == "sk-original"


class TestAuthFieldFiltering:
    def _get_form_fields(self, platform):
        from codebase.base import GitPlatform
        from core.forms import SiteConfigurationForm
        from core.models import SiteConfiguration

        config = SiteConfiguration.objects.get_instance()
        with patch("codebase.conf.settings") as mock_codebase:
            mock_codebase.CLIENT = getattr(GitPlatform, platform)
            form = SiteConfigurationForm(instance=config)
        return set(form.fields.keys())

    def test_gitlab_shows_all_auth_fields(self, db):
        fields = self._get_form_fields("GITLAB")
        assert "auth_login_enabled" in fields
        assert "auth_client_id" in fields
        assert "auth_client_secret" in fields
        assert "auth_gitlab_url" in fields
        assert "auth_gitlab_server_url" in fields

    def test_github_hides_gitlab_specific_fields(self, db):
        fields = self._get_form_fields("GITHUB")
        assert "auth_login_enabled" in fields
        assert "auth_client_id" in fields
        assert "auth_client_secret" in fields
        assert "auth_gitlab_url" not in fields
        assert "auth_gitlab_server_url" not in fields

    def test_swe_hides_all_auth_fields(self, db):
        fields = self._get_form_fields("SWE")
        assert "auth_login_enabled" not in fields
        assert "auth_client_id" not in fields
        assert "auth_client_secret" not in fields
        assert "auth_gitlab_url" not in fields
        assert "auth_gitlab_server_url" not in fields


class TestAuthCredentialValidation:
    def _make_form(self, data, *, instance=None, cleared_secrets=None, env_locked_fields=None):
        from codebase.base import GitPlatform
        from core.forms import SiteConfigurationForm
        from core.models import SiteConfiguration

        config = instance or SiteConfiguration.objects.get_instance()
        with patch("codebase.conf.settings") as mock_codebase:
            mock_codebase.CLIENT = GitPlatform.GITHUB
            return SiteConfigurationForm(
                data=data,
                instance=config,
                cleared_secrets=cleared_secrets or set(),
                env_locked_fields=env_locked_fields or set(),
            )

    def test_client_id_without_secret_rejected(self, db):
        form = self._make_form({"auth_client_id": "my-id"})
        assert not form.is_valid()
        assert "auth_client_secret" in form.errors

    def test_secret_without_client_id_rejected(self, db):
        form = self._make_form({"auth_client_secret": "my-secret"})
        assert not form.is_valid()
        assert "auth_client_id" in form.errors

    def test_both_present_accepted(self, db):
        form = self._make_form({"auth_client_id": "my-id", "auth_client_secret": "my-secret"})
        assert "auth_client_id" not in form.errors
        assert "auth_client_secret" not in form.errors

    def test_neither_present_accepted(self, db):
        form = self._make_form({})
        assert "auth_client_id" not in form.errors
        assert "auth_client_secret" not in form.errors

    def test_secret_in_db_with_client_id_in_form_accepted(self, db):
        from core.models import SiteConfiguration

        config = SiteConfiguration.objects.get_instance()
        config.auth_client_secret = "existing-secret"  # noqa: S105
        config.save()

        form = self._make_form({"auth_client_id": "my-id"}, instance=config)
        assert "auth_client_id" not in form.errors
        assert "auth_client_secret" not in form.errors

    def test_client_id_in_db_with_secret_in_form_accepted(self, db):
        from core.models import SiteConfiguration

        config = SiteConfiguration.objects.get_instance()
        config.auth_client_id = "existing-id"
        config.save()

        form = self._make_form({"auth_client_secret": "my-secret"}, instance=config)
        assert "auth_client_id" not in form.errors
        assert "auth_client_secret" not in form.errors

    def test_clearing_secret_while_client_id_set_rejected(self, db):
        from core.models import SiteConfiguration

        config = SiteConfiguration.objects.get_instance()
        config.auth_client_id = "existing-id"
        config.auth_client_secret = "existing-secret"  # noqa: S105
        config.save()

        form = self._make_form(
            {"auth_client_id": "existing-id"}, instance=config, cleared_secrets={"auth_client_secret"}
        )
        assert not form.is_valid()
        assert "auth_client_secret" in form.errors


class TestInFlightProviderValidation:
    """Coverage for the in-flight provider map: model-name validation must see
    the *intent* of the current POST, not the pre-save DB state.

    With the per-group split, these scenarios are validated at the form level
    (SiteConfigurationForm accepts ``in_flight_providers`` directly).
    """

    def test_enabled_provider_with_key_accepted_via_in_flight(self, db):
        """Passing an in-flight map that marks a provider enabled+keyed lets the
        model-name field pass even when the DB row is disabled."""
        from core.forms import SiteConfigurationForm

        provider = Provider.objects.get(slug="anthropic")
        provider.is_enabled = False
        provider.api_key = None
        provider.save()

        config = SiteConfiguration.objects.get_instance()
        form = SiteConfigurationForm(
            data={"agent_model_name_provider": "anthropic", "agent_model_name_model": "claude-sonnet-4-6"},
            instance=config,
            in_flight_providers={"anthropic": (True, True)},
        )
        assert form.is_valid(), f"Form errors: {form.errors}"

    def test_in_flight_disabled_provider_rejected(self, db):
        """An in-flight map that marks a provider disabled must still reject it."""
        from core.forms import SiteConfigurationForm

        _enable_seed_provider("anthropic")
        config = SiteConfiguration.objects.get_instance()
        form = SiteConfigurationForm(
            data={"agent_model_name_provider": "anthropic", "agent_model_name_model": "claude-sonnet-4-6"},
            instance=config,
            in_flight_providers={"anthropic": (False, True)},
        )
        assert not form.is_valid()
        assert "agent_model_name" in form.errors
        assert "disabled" in str(form.errors["agent_model_name"][0])

    def test_in_flight_clear_api_key_rejected(self, db):
        """An in-flight map with has_key=False (clear_api_key) must reject the model."""
        from core.forms import SiteConfigurationForm

        _enable_seed_provider("anthropic", api_key="sk-existing")
        config = SiteConfiguration.objects.get_instance()
        form = SiteConfigurationForm(
            data={"agent_model_name_provider": "anthropic", "agent_model_name_model": "claude-sonnet-4-6"},
            instance=config,
            in_flight_providers={"anthropic": (True, False)},
        )
        assert not form.is_valid()
        assert "agent_model_name" in form.errors

    def test_in_flight_new_provider_accepted(self, db):
        """A slug that doesn't exist in the DB yet but appears in in_flight_providers
        must be accepted."""
        from core.forms import SiteConfigurationForm

        config = SiteConfiguration.objects.get_instance()
        form = SiteConfigurationForm(
            data={"agent_model_name_provider": "custom-router", "agent_model_name_model": "my-model"},
            instance=config,
            in_flight_providers={"custom-router": (True, True)},
        )
        assert form.is_valid(), f"Form errors: {form.errors}"


class TestSiteConfigurationFormScoping:
    """When constructed with a ``group=`` kwarg, the form must only expose that group's fields."""

    def test_scoped_to_agent_drops_other_fields(self, db):
        from core.forms import SiteConfigurationForm
        from core.models import SiteConfiguration

        agent_group = next(g for g in SiteConfiguration.get_field_groups() if g.key == "agent")
        form = SiteConfigurationForm(instance=SiteConfiguration.objects.get_instance(), group=agent_group)
        # Agent fields present
        assert "agent_model_name" in form.fields
        assert "agent_recursion_limit" in form.fields
        # Non-agent fields removed
        assert "sandbox_timeout" not in form.fields
        assert "web_fetch_enabled" not in form.fields
        assert "rocketchat_enabled" not in form.fields

    def test_unscoped_keeps_all_fields(self, db):
        from core.forms import SiteConfigurationForm
        from core.models import SiteConfiguration

        form = SiteConfigurationForm(instance=SiteConfiguration.objects.get_instance())
        # Spot-check: at least one field from each top-level group is present
        for name in ("agent_model_name", "sandbox_timeout", "web_fetch_enabled"):
            assert name in form.fields


class TestRoutingAndAccess:
    def test_index_redirects_to_first_group(self, client, admin_user, index_url, group_url):
        client.force_login(admin_user)
        response = client.get(index_url)
        assert response.status_code == 302
        assert response.url == group_url("agent")

    def test_unknown_group_key_returns_404(self, client, admin_user, group_url):
        client.force_login(admin_user)
        response = client.get(group_url("does-not-exist"))
        assert response.status_code == 404

    def test_member_blocked_on_subpage(self, client, member_user, group_url):
        client.force_login(member_user)
        response = client.get(group_url("agent"))
        assert response.status_code == 403

    def test_member_blocked_on_index(self, client, member_user, index_url):
        client.force_login(member_user)
        response = client.get(index_url)
        assert response.status_code == 403


class TestPerGroupContent:
    def test_agent_page_renders_agent_fields_only(self, client, admin_user, group_url):
        client.force_login(admin_user)
        response = client.get(group_url("agent"))
        content = response.content.decode()
        assert "Agent" in content
        # Field labels from other groups must not appear
        assert "id_sandbox_timeout" not in content
        assert "id_web_fetch_enabled" not in content
        assert "id_rocketchat_url" not in content

    def test_sandbox_page_renders_sandbox_fields_only(self, client, admin_user, group_url):
        client.force_login(admin_user)
        response = client.get(group_url("sandbox"))
        content = response.content.decode()
        assert "id_sandbox_timeout" in content
        assert "id_agent_model_name" not in content

    def test_rail_shows_all_groups_with_active_marked(self, client, admin_user, group_url):
        client.force_login(admin_user)
        response = client.get(group_url("agent"))
        content = response.content.decode()
        # Every group title appears in the rail (& is HTML-escaped to &amp; in templates)
        for title in (
            "Agent",
            "Commit &amp; PR Writer",
            "Titling",
            "Providers",
            "Web Search",
            "Web Fetch",
            "Sandbox",
            "Jobs",
            "Rocket Chat",
            "Authentication",
        ):
            assert title in content, f"Rail missing {title!r}"
        # Category headers appear
        for category in ("AI TASKS", "MODELS", "AGENT TOOLS", "RUNTIME", "INTEGRATIONS"):
            assert category in content.upper(), f"Rail missing category {category!r}"


class TestPerGroupSaveIsolation:
    def test_post_agent_does_not_clobber_sandbox(self, client, admin_user, group_url):
        # Arrange: set a non-default sandbox_timeout via the model directly
        config = SiteConfiguration.objects.get_instance()
        config.sandbox_timeout = 1234
        config.save()

        _enable_seed_provider("anthropic")
        client.force_login(admin_user)
        response = client.post(
            group_url("agent"),
            {
                "agent_model_name_provider": "anthropic",
                "agent_model_name_model": "claude-sonnet-4-6",
                "agent_recursion_limit": 250,
            },
        )
        assert response.status_code == 302

        config.refresh_from_db()
        # Agent field updated
        assert config.agent_model_name == "anthropic:claude-sonnet-4-6"
        assert config.agent_recursion_limit == 250
        # Sandbox field untouched
        assert config.sandbox_timeout == 1234

    def test_post_invalid_agent_re_renders_and_persists_nothing(self, client, admin_user, group_url):
        config = SiteConfiguration.objects.get_instance()
        config.agent_recursion_limit = 500
        config.save()

        client.force_login(admin_user)
        response = client.post(
            group_url("agent"),
            {"agent_recursion_limit": "-7"},  # PositiveIntegerField rejects negatives
        )
        assert response.status_code == 200

        config.refresh_from_db()
        assert config.agent_recursion_limit == 500  # unchanged

    def test_post_providers_saves_through_formset_and_invalidates_cache(self, client, admin_user, group_url):
        from django.core.cache import cache

        from core.models import PROVIDERS_CACHE_KEY

        # Warm the cache so we can verify it gets invalidated
        Provider.get_cached_rows()
        assert cache.get(PROVIDERS_CACHE_KEY) is not None

        client.force_login(admin_user)
        mgmt = _providers_mgmt()
        # Toggle the first row's display_name to force a save
        first_pk = Provider.objects.order_by("sort_order", "slug").first().pk
        idx = next(i for i, p in enumerate(Provider.objects.order_by("sort_order", "slug")) if p.pk == first_pk)
        mgmt[f"providers-{idx}-display_name"] = "Renamed-In-Test"

        response = client.post(group_url("providers"), mgmt)
        assert response.status_code == 302

        Provider.objects.get(pk=first_pk)  # exists
        assert Provider.objects.get(pk=first_pk).display_name == "Renamed-In-Test"
        # Cache invalidated by Provider.save signal / formset save
        assert cache.get(PROVIDERS_CACHE_KEY) is None

    def test_enable_provider_then_select_model_on_agent_page(self, client, admin_user, group_url):
        """Two-step flow: enable a fresh provider on /providers/, then select its model on /agent/."""
        provider = Provider.objects.get(slug="anthropic")
        provider.is_enabled = False
        provider.api_key = None
        provider.save()

        client.force_login(admin_user)

        # Step 1: enable + key the provider via the providers page
        mgmt = _providers_mgmt()
        idx = next(i for i, p in enumerate(Provider.objects.order_by("sort_order", "slug")) if p.pk == provider.pk)
        mgmt[f"providers-{idx}-is_enabled"] = "on"
        mgmt[f"providers-{idx}-api_key"] = "sk-fresh"
        r1 = client.post(group_url("providers"), mgmt)
        assert r1.status_code == 302

        # Step 2: select that provider's model on the agent page
        r2 = client.post(
            group_url("agent"),
            {
                "agent_model_name_provider": "anthropic",
                "agent_model_name_model": "claude-sonnet-4-6",
                "agent_recursion_limit": 200,
            },
        )
        assert r2.status_code == 302

        config = SiteConfiguration.objects.get_instance()
        assert config.agent_model_name == "anthropic:claude-sonnet-4-6"
        assert config.agent_recursion_limit == 200
