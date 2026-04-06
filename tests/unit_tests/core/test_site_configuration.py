from unittest.mock import patch

from django.core.cache import cache as django_cache

import pytest

from core.models import SITE_CONFIGURATION_CACHE_KEY, SiteConfiguration


@pytest.fixture
def site_config(db):
    """Create and return a SiteConfiguration instance."""
    return SiteConfiguration.objects.get_instance()


class TestSingletonBehavior:
    def test_get_instance_creates_singleton(self, db):
        instance = SiteConfiguration.objects.get_instance()
        assert instance.pk == 1

    def test_get_instance_returns_same_row(self, db):
        first = SiteConfiguration.objects.get_instance()
        second = SiteConfiguration.objects.get_instance()
        assert first.pk == second.pk

    def test_save_forces_pk_1(self, db):
        config = SiteConfiguration()
        config.pk = 999
        config.save()
        assert config.pk == 1

    def test_delete_raises(self, site_config):
        with pytest.raises(RuntimeError, match="cannot be deleted"):
            site_config.delete()


class TestCaching:
    def test_get_cached_returns_instance(self, db):
        instance = SiteConfiguration.get_cached()
        assert instance.pk == 1

    async def test_get_cached_works_in_async_context_cold_cache(self, db):
        """When called from an async context with cold cache, fetches via thread pool."""
        django_cache.delete(SITE_CONFIGURATION_CACHE_KEY)
        result = SiteConfiguration.get_cached()
        assert result is not None
        assert result.pk == 1

    async def test_get_cached_populates_cache_in_async_context(self, db):
        """Cold cache async fetch should populate cache for subsequent calls."""
        django_cache.delete(SITE_CONFIGURATION_CACHE_KEY)
        SiteConfiguration.get_cached()
        cached = django_cache.get(SITE_CONFIGURATION_CACHE_KEY)
        assert cached is not None
        assert cached.pk == 1

    async def test_get_cached_works_in_async_context_warm_cache(self, site_config):
        """When cache is warm, works fine in an async context."""
        django_cache.set(SITE_CONFIGURATION_CACHE_KEY, site_config)
        result = SiteConfiguration.get_cached()
        assert result is not None
        assert result.pk == 1

    async def test_get_cached_returns_none_on_db_failure_in_async_context(self, db):
        """DB failure in async context returns None gracefully."""
        django_cache.delete(SITE_CONFIGURATION_CACHE_KEY)
        with patch.object(SiteConfiguration.objects, "get_instance", side_effect=Exception("DB down")):
            result = SiteConfiguration.get_cached()
        assert result is None

    async def test_get_cached_returns_none_on_timeout_in_async_context(self, db):
        """Thread pool timeout in async context returns None gracefully."""
        import time

        django_cache.delete(SITE_CONFIGURATION_CACHE_KEY)
        with patch.object(SiteConfiguration, "_fetch_from_cache_or_db", side_effect=lambda: time.sleep(10)):
            result = SiteConfiguration.get_cached()
        assert result is None

    def test_save_invalidates_cache(self, site_config):
        with patch("core.models.cache") as mock_cache:
            site_config.agent_model_name = "test-model"
            site_config.save()
            mock_cache.delete.assert_called_once_with("site_configuration")


class TestNullableFields:
    def test_all_fields_null_by_default(self, site_config):
        assert site_config.agent_model_name is None
        assert site_config.agent_thinking_level is None
        assert site_config.web_search_enabled is None
        assert site_config.sandbox_timeout is None
        assert site_config.jobs_throttle_rate is None

    def test_set_and_read_plain_fields(self, site_config):
        site_config.agent_model_name = "test-model"
        site_config.web_search_enabled = False
        site_config.agent_recursion_limit = 100
        site_config.save()

        reloaded = SiteConfiguration.objects.get_instance()
        assert reloaded.agent_model_name == "test-model"
        assert reloaded.web_search_enabled is False
        assert reloaded.agent_recursion_limit == 100


class TestEncryptedFields:
    def test_set_and_read_encrypted_field(self, site_config):
        site_config.anthropic_api_key = "sk-test-12345"
        site_config.save()

        reloaded = SiteConfiguration.objects.get_instance()
        assert reloaded.anthropic_api_key == "sk-test-12345"
        # The raw DB column should be encrypted
        assert reloaded._anthropic_api_key_encrypted is not None
        assert reloaded._anthropic_api_key_encrypted != "sk-test-12345"

    def test_clear_encrypted_field(self, site_config):
        site_config.anthropic_api_key = "sk-test-12345"
        site_config.save()

        site_config.anthropic_api_key = None
        site_config.save()

        reloaded = SiteConfiguration.objects.get_instance()
        assert reloaded.anthropic_api_key is None
        assert reloaded._anthropic_api_key_encrypted is None

    def test_set_empty_string_clears_field(self, site_config):
        site_config.anthropic_api_key = "sk-test-12345"
        site_config.save()

        site_config.anthropic_api_key = ""
        site_config.save()

        reloaded = SiteConfiguration.objects.get_instance()
        assert reloaded.anthropic_api_key is None

    def test_get_secret_hint(self, site_config):
        site_config.anthropic_api_key = "sk-test-long-api-key-12345"
        site_config.save()

        hint = site_config.get_secret_hint("anthropic_api_key")
        assert hint is not None
        assert "sk-" in hint
        assert "345" in hint
        assert hint != "sk-test-long-api-key-12345"

    def test_get_secret_hint_when_not_set(self, site_config):
        assert site_config.get_secret_hint("anthropic_api_key") is None

    def test_all_encrypted_fields_listed(self):
        assert "anthropic_api_key" in SiteConfiguration.ENCRYPTED_FIELDS
        assert "openai_api_key" in SiteConfiguration.ENCRYPTED_FIELDS
        assert "google_api_key" in SiteConfiguration.ENCRYPTED_FIELDS
        assert "openrouter_api_key" in SiteConfiguration.ENCRYPTED_FIELDS
        assert "web_search_api_key" in SiteConfiguration.ENCRYPTED_FIELDS
        assert "sandbox_api_key" in SiteConfiguration.ENCRYPTED_FIELDS


class TestGetFieldGroups:
    def test_all_configurable_fields_assigned(self):
        """Every model field and encrypted field must appear in exactly one group."""
        groups = SiteConfiguration.get_field_groups()
        all_assigned = []
        for group in groups:
            all_assigned.extend(group.fields)

        # Every plain configurable model field should be assigned
        plain_fields = [
            f.name
            for f in SiteConfiguration._meta.get_fields()
            if f.name != "id" and not f.name.startswith("_") and f.concrete
        ]
        for name in plain_fields:
            assert name in all_assigned, f"Field '{name}' not assigned to any group"

        # Every encrypted field should be assigned
        for name in SiteConfiguration.ENCRYPTED_FIELDS:
            assert name in all_assigned, f"Encrypted field '{name}' not assigned to any group"

    def test_no_duplicate_assignments(self):
        groups = SiteConfiguration.get_field_groups()
        seen = set()
        for group in groups:
            for name in group.fields:
                assert name not in seen, f"Field '{name}' assigned to multiple groups"
                seen.add(name)

    def test_expected_groups_exist(self):
        groups = SiteConfiguration.get_field_groups()
        keys = [g.key for g in groups]
        assert "agent" in keys
        assert "providers" in keys
        assert "sandbox" in keys
        assert "jobs" in keys
