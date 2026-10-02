"""``automation.signals`` drops a provider's model-catalog cache entry when the provider changes."""

from __future__ import annotations

import contextlib

from django.core.cache import cache
from django.db import transaction

import pytest

from automation.agent.model_catalog.cache_keys import MODEL_CATALOG_CACHE_KEY_FMT
from core.models import Provider, ProviderType

KEY = MODEL_CATALOG_CACHE_KEY_FMT.format(slug="customprov")


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def provider() -> Provider:
    provider = Provider.objects.create(
        slug="customprov", display_name="Custom", provider_type=ProviderType.OPENAI, api_key="sk-test"
    )
    cache.set(KEY, "warm", 60)
    return provider


@pytest.mark.django_db
def test_save_clears_catalog_key_on_commit_not_before(provider, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks() as callbacks:
        provider.display_name = "Custom Updated"
        provider.save()
        assert cache.get(KEY) == "warm"

    for callback in callbacks:
        callback()
    assert cache.get(KEY) is None


@pytest.mark.django_db
def test_delete_clears_catalog_key_on_commit_not_before(provider, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks() as callbacks:
        provider.delete()
        assert cache.get(KEY) == "warm"

    for callback in callbacks:
        callback()
    assert cache.get(KEY) is None


@pytest.mark.django_db
def test_rolled_back_save_keeps_catalog_key(provider, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True), contextlib.suppress(RuntimeError), transaction.atomic():
        provider.display_name = "Custom Updated"
        provider.save()
        raise RuntimeError

    assert cache.get(KEY) == "warm"


@pytest.mark.django_db
def test_queryset_delete_clears_catalog_key(provider, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        Provider.objects.filter(slug="customprov").delete()

    assert cache.get(KEY) is None
