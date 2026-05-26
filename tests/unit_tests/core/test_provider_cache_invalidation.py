"""Verify Provider.save() and .delete() invalidate the model catalog cache."""

from __future__ import annotations

from django.core.cache import cache

import pytest

from automation.agent.model_catalog.service import MODEL_CATALOG_CACHE_KEY_FMT, CatalogEntry
from core.models import Provider, ProviderType


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db(transaction=True)
def test_save_invalidates_model_catalog_cache():
    Provider.objects.filter(slug="openrouter").delete()
    Provider.objects.create(
        slug="openrouter",
        display_name="OpenRouter",
        provider_type=ProviderType.OPENROUTER,
        api_key="sk-test",
        is_enabled=True,
    )

    key = MODEL_CATALOG_CACHE_KEY_FMT.format(slug="openrouter")
    cache.set(key, CatalogEntry(models=["x"], error=None), 60)
    assert cache.get(key) is not None

    row = Provider.objects.get(slug="openrouter")
    row.display_name = "OpenRouter Updated"
    row.save()

    assert cache.get(key) is None, "expected save() to invalidate model catalog cache"


@pytest.mark.django_db(transaction=True)
def test_delete_invalidates_model_catalog_cache():
    Provider.objects.filter(slug="customprov").delete()
    Provider.objects.create(
        slug="customprov",
        display_name="Custom",
        provider_type=ProviderType.OPENAI,
        api_key="sk-test",
        is_enabled=True,
        is_locked=False,
    )

    key = MODEL_CATALOG_CACHE_KEY_FMT.format(slug="customprov")
    cache.set(key, CatalogEntry(models=["y"], error=None), 60)

    Provider.objects.get(slug="customprov").delete()

    assert cache.get(key) is None
