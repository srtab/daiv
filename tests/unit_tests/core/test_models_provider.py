from unittest.mock import patch

from django.core.cache import cache
from django.db import transaction

import pytest
from pydantic import SecretStr

from core.models import PROVIDERS_CACHE_KEY, Provider, ProviderType


@pytest.mark.django_db
def test_provider_save_encrypts_api_key():
    p = Provider.objects.create(
        slug="myprov", display_name="My", provider_type=ProviderType.OPENAI, api_key="sk-secret"
    )
    p.refresh_from_db()
    assert p._api_key_encrypted is not None
    assert p._api_key_encrypted != "sk-secret"
    assert p.api_key == "sk-secret"


@pytest.mark.django_db
def test_locked_row_blocks_slug_change():
    p = Provider.objects.get(slug="anthropic")
    assert p.is_locked is True
    p.slug = "renamed"
    with pytest.raises(ValueError, match="locked"):
        p.save()


@pytest.mark.django_db
def test_locked_row_blocks_provider_type_change():
    p = Provider.objects.get(slug="anthropic")
    assert p.is_locked is True
    p.provider_type = ProviderType.OPENAI
    with pytest.raises(ValueError, match="locked"):
        p.save()


@pytest.mark.django_db
def test_locked_row_allows_toggle_enabled_and_key_edit():
    p = Provider.objects.get(slug="openai")
    assert p.is_locked is True
    p.is_enabled = False
    p.api_key = "k2"
    p.display_name = "OpenAI (Org A)"
    p.save()
    p.refresh_from_db()
    assert p.is_enabled is False
    assert p.api_key == "k2"
    assert p.display_name == "OpenAI (Org A)"


@pytest.mark.django_db
def test_get_cached_returns_secretstr():
    Provider.objects.create(slug="myprov", display_name="My", provider_type=ProviderType.OPENAI, api_key="sk")
    rows = Provider.get_cached_rows()
    matching = [r for r in rows if r.slug == "myprov"]
    assert len(matching) == 1
    row = matching[0]
    assert isinstance(row.api_key, SecretStr)
    assert row.api_key.get_secret_value() == "sk"


@pytest.mark.django_db
def test_save_invalidates_cache(django_capture_on_commit_callbacks):
    # Invalidation is deferred via transaction.on_commit so concurrent readers
    # can't repopulate the cache with pre-commit data; execute=True runs the
    # callback at the boundary just like a real commit would.
    with django_capture_on_commit_callbacks(execute=True):
        Provider.objects.create(slug="a", display_name="A", provider_type=ProviderType.OPENAI, api_key="k")
    slugs_before = {r.slug for r in Provider.get_cached_rows()}
    assert "a" in slugs_before
    with django_capture_on_commit_callbacks(execute=True):
        Provider.objects.create(slug="b", display_name="B", provider_type=ProviderType.OPENAI, api_key="k")
    slugs_after = {r.slug for r in Provider.get_cached_rows()}
    assert "a" in slugs_after
    assert "b" in slugs_after


@pytest.mark.django_db(transaction=True)
def test_save_defers_cache_invalidation_until_commit():
    """
    Inside an atomic block, cache.delete must NOT fire until after commit; otherwise
    a concurrent reader on another connection re-caches pre-commit data and masks
    the change for up to PROVIDERS_CACHE_TIMEOUT.
    """
    Provider.objects.create(slug="defer", display_name="D", provider_type=ProviderType.OPENAI, api_key="k")
    cache.set(PROVIDERS_CACHE_KEY, "warm-marker", 300)

    delete_in_atomic: list[bool] = []
    original_delete = cache.delete

    def tracking_delete(key, *a, **kw):
        if key == PROVIDERS_CACHE_KEY:
            delete_in_atomic.append(transaction.get_connection().in_atomic_block)
        return original_delete(key, *a, **kw)

    with patch.object(cache, "delete", side_effect=tracking_delete), transaction.atomic():
        p = Provider.objects.get(slug="defer")
        p.verify_ssl = False
        p.save()
        # Still inside the atomic block: nothing should have invalidated yet.
        assert delete_in_atomic == []
        assert cache.get(PROVIDERS_CACHE_KEY) == "warm-marker"

    # After commit: invalidation runs exactly once, and the connection is no longer in_atomic.
    assert delete_in_atomic == [False]
    assert cache.get(PROVIDERS_CACHE_KEY) is None


@pytest.mark.django_db
def test_delete_invalidates_cache(django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        p = Provider.objects.create(slug="zdelme", display_name="A", provider_type=ProviderType.OPENAI, api_key="k")
    assert "zdelme" in {r.slug for r in Provider.get_cached_rows()}
    with django_capture_on_commit_callbacks(execute=True):
        p.delete()
    assert "zdelme" not in {r.slug for r in Provider.get_cached_rows()}


@pytest.mark.django_db
def test_delete_locked_row_raises():
    p = Provider.objects.create(
        slug="zlocked", display_name="Z Locked", provider_type=ProviderType.OPENAI, is_locked=True
    )
    with pytest.raises(ValueError, match="locked"):
        p.delete()


@pytest.mark.django_db(transaction=True)
async def test_get_cached_rows_in_async_context():
    """Agent dispatch reads providers from async paths; regression must not raise SynchronousOnlyOperation."""
    from asgiref.sync import sync_to_async

    @sync_to_async
    def setup():
        Provider.objects.create(slug="vasync", display_name="V", provider_type=ProviderType.OPENAI, api_key="k")
        Provider.invalidate_cache()

    await setup()
    # Calling from inside a running loop must work — exercises the executor hop.
    rows_async = Provider.get_cached_rows()
    assert "vasync" in {r.slug for r in rows_async}


@pytest.mark.django_db
def test_cached_provider_type_is_enum():
    """The cached snapshot exposes provider_type as a ProviderType, not a raw string."""
    rows = Provider.get_cached_rows()
    for row in rows:
        assert isinstance(row.provider_type, ProviderType)


@pytest.mark.django_db
def test_cached_carries_use_responses_api():
    Provider.objects.create(
        slug="resp", display_name="Resp", provider_type=ProviderType.OPENAI, api_key="k", use_responses_api=True
    )
    row = next(r for r in Provider.get_cached_rows() if r.slug == "resp")
    assert row.use_responses_api is True


@pytest.mark.django_db
def test_cached_carries_verify_ssl_default_true():
    Provider.objects.create(slug="vrf", display_name="V", provider_type=ProviderType.OPENAI, api_key="k")
    row = next(r for r in Provider.get_cached_rows() if r.slug == "vrf")
    assert row.verify_ssl is True


@pytest.mark.django_db
def test_cached_carries_verify_ssl_disabled():
    Provider.objects.create(
        slug="insec", display_name="Insec", provider_type=ProviderType.OPENAI, api_key="k", verify_ssl=False
    )
    row = next(r for r in Provider.get_cached_rows() if r.slug == "insec")
    assert row.verify_ssl is False
