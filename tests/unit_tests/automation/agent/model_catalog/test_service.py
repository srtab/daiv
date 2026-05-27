"""Tests for the catalog service: cache, concurrency, error mapping."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from django.core.cache import cache

import pytest
from pydantic import SecretStr

from automation.agent.model_catalog.exceptions import (
    CatalogFetchError,
    MissingApiKeyError,
    UnsupportedProviderTypeError,
)
from automation.agent.model_catalog.service import MODEL_CATALOG_CACHE_KEY_FMT, CatalogEntry, fetch_catalog
from core.models import Provider, ProviderType


def _row(slug: str, provider_type: ProviderType = ProviderType.OPENROUTER) -> Provider.Cached:
    return Provider.Cached(
        slug=slug,
        display_name=slug.title(),
        provider_type=provider_type,
        base_url="",
        api_key=SecretStr("sk-test"),
        extra_headers={},
        is_enabled=True,
        use_responses_api=False,
        verify_ssl=True,
        is_locked=False,
        sort_order=0,
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


async def test_returns_entry_per_row():
    rows = [_row("openrouter", ProviderType.OPENROUTER), _row("anthropic", ProviderType.ANTHROPIC)]
    fake_or = AsyncMock(return_value=["anthropic/claude-haiku-4.5"])
    fake_an = AsyncMock(return_value=["claude-opus-4.6"])

    with patch("automation.agent.model_catalog.service.get_adapter") as get_adapter:
        get_adapter.side_effect = lambda pt: type(
            "A", (), {"list_models": fake_or if pt == ProviderType.OPENROUTER else fake_an}
        )()

        result = await fetch_catalog(rows)

    assert result == {
        "openrouter": CatalogEntry(models=["anthropic/claude-haiku-4.5"], error=None),
        "anthropic": CatalogEntry(models=["claude-opus-4.6"], error=None),
    }


async def test_cache_hit_skips_adapter():
    row = _row("openrouter")
    cache.set(
        MODEL_CATALOG_CACHE_KEY_FMT.format(slug="openrouter"), CatalogEntry(models=["cached-model"], error=None), 60
    )
    fake_adapter = AsyncMock()
    with patch("automation.agent.model_catalog.service.get_adapter") as get_adapter:
        get_adapter.return_value.list_models = fake_adapter
        result = await fetch_catalog([row])

    assert result["openrouter"].models == ["cached-model"]
    fake_adapter.assert_not_called()


async def test_cache_populated_on_success():
    row = _row("openrouter")
    fake_adapter = AsyncMock(return_value=["m1", "m2"])
    with patch("automation.agent.model_catalog.service.get_adapter") as get_adapter:
        get_adapter.return_value.list_models = fake_adapter
        await fetch_catalog([row])

    cached = cache.get(MODEL_CATALOG_CACHE_KEY_FMT.format(slug="openrouter"))
    assert cached == CatalogEntry(models=["m1", "m2"], error=None)


async def test_cache_not_populated_on_error():
    row = _row("openrouter")
    fake_adapter = AsyncMock(side_effect=CatalogFetchError("upstream down"))
    with patch("automation.agent.model_catalog.service.get_adapter") as get_adapter:
        get_adapter.return_value.list_models = fake_adapter
        result = await fetch_catalog([row])

    assert result["openrouter"].models == []
    assert result["openrouter"].error == "upstream down"
    assert cache.get(MODEL_CATALOG_CACHE_KEY_FMT.format(slug="openrouter")) is None


async def test_missing_api_key_mapped_to_safe_string():
    row = _row("openrouter")
    fake_adapter = AsyncMock(side_effect=MissingApiKeyError("Provider 'openrouter' has no API key"))
    with patch("automation.agent.model_catalog.service.get_adapter") as get_adapter:
        get_adapter.return_value.list_models = fake_adapter
        result = await fetch_catalog([row])

    assert result["openrouter"].models == []
    assert result["openrouter"].error == "API key not configured"


async def test_unsupported_provider_type_mapped():
    row = _row("weird")
    with patch("automation.agent.model_catalog.service.get_adapter") as get_adapter:
        get_adapter.side_effect = UnsupportedProviderTypeError("no adapter")
        result = await fetch_catalog([row])

    assert result["weird"].models == []
    assert result["weird"].error == "unsupported provider type"


async def test_per_provider_timeout_mapped():
    row = _row("slow")

    async def _hang(_row):
        await asyncio.sleep(10)
        return []

    with (
        patch("automation.agent.model_catalog.service.get_adapter") as get_adapter,
        patch("automation.agent.model_catalog.service.MODEL_CATALOG_FETCH_TIMEOUT", 0.05),
    ):
        get_adapter.return_value.list_models = _hang
        result = await fetch_catalog([row])

    assert result["slow"].models == []
    assert result["slow"].error == "Request timed out"


async def test_one_failing_provider_doesnt_block_others():
    rows = [_row("good"), _row("bad")]

    async def _good(_row):
        return ["m1"]

    async def _bad(_row):
        raise CatalogFetchError("nope")

    with patch("automation.agent.model_catalog.service.get_adapter") as get_adapter:

        class _Routing:
            async def list_models(self, row):
                if row.slug == "good":
                    return await _good(row)
                return await _bad(row)

        get_adapter.return_value = _Routing()
        result = await fetch_catalog(rows)

    assert result["good"].models == ["m1"]
    assert result["good"].error is None
    assert result["bad"].models == []
    assert result["bad"].error == "nope"


async def test_total_timeout_cancels_inflight_tasks_and_marks_them_timed_out():
    """When MODEL_CATALOG_TOTAL_TIMEOUT fires before per-provider tasks finish,
    rows still in flight get ``error='Request timed out'`` and their tasks are cancelled.

    Patches MODEL_CATALOG_FETCH_TIMEOUT higher than the total so the inner
    per-provider timeout does not fire first — this is the only path that exercises
    the outer gather timeout + the task.cancel() branch.
    """
    fast_row = _row("fast")
    slow_row = _row("slow")
    cancelled = asyncio.Event()

    async def _list_models(row):
        if row.slug == "fast":
            return ["fast-model"]
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return []

    class _Adapter:
        async def list_models(self, row):
            return await _list_models(row)

    adapter = _Adapter()
    with (
        patch("automation.agent.model_catalog.service.get_adapter", return_value=adapter),
        patch("automation.agent.model_catalog.service.MODEL_CATALOG_FETCH_TIMEOUT", 5.0),
        patch("automation.agent.model_catalog.service.MODEL_CATALOG_TOTAL_TIMEOUT", 0.05),
    ):
        result = await fetch_catalog([fast_row, slow_row])

    assert result["fast"].models == ["fast-model"]
    assert result["fast"].error is None
    assert result["slow"].models == []
    assert result["slow"].error == "Request timed out"
    # Cancel grace gave the slow task a chance to observe CancelledError.
    assert cancelled.is_set()


async def test_empty_response_is_successful_and_cached():
    row = _row("openrouter")
    fake_adapter = AsyncMock(return_value=[])
    with patch("automation.agent.model_catalog.service.get_adapter") as get_adapter:
        get_adapter.return_value.list_models = fake_adapter
        result = await fetch_catalog([row])

    assert result["openrouter"] == CatalogEntry(models=[], error=None)
    assert cache.get(MODEL_CATALOG_CACHE_KEY_FMT.format(slug="openrouter")) == CatalogEntry(models=[], error=None)
