"""Tests for per-provider catalog adapters.

SDK calls are mocked at the AsyncOpenAI / AsyncAnthropic / google.genai.Client
boundary — we never hit real APIs. The Provider.Cached rows are constructed
directly without DB.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from automation.agent.model_catalog.adapters import AnthropicAdapter, OpenAIAdapter
from automation.agent.model_catalog.exceptions import CatalogFetchError, MissingApiKeyError
from core.models import Provider, ProviderType


def _row(
    *,
    slug: str = "openai",
    provider_type: ProviderType = ProviderType.OPENAI,
    base_url: str = "",
    api_key: str | None = "sk-test",
    verify_ssl: bool = True,
) -> Provider.Cached:
    return Provider.Cached(
        slug=slug,
        display_name=slug.title(),
        provider_type=provider_type,
        base_url=base_url,
        api_key=SecretStr(api_key) if api_key else None,
        extra_headers={},
        is_enabled=True,
        use_responses_api=False,
        verify_ssl=verify_ssl,
        is_locked=False,
        sort_order=0,
    )


def _async_list_iter(items):
    """Build an async-iterable mock matching the openai/anthropic ``models.list()`` shape."""

    async def _aiter(_self):
        for item in items:
            yield item

    mock = MagicMock()
    mock.__aiter__ = _aiter
    return mock


class TestOpenAIAdapter:
    async def test_returns_sorted_filtered_ids(self):
        items = [
            SimpleNamespace(id="gpt-5.4"),
            SimpleNamespace(id="gpt-4o"),
            SimpleNamespace(id="text-embedding-3-large"),
            SimpleNamespace(id="whisper-1"),
            SimpleNamespace(id="dall-e-3"),
            SimpleNamespace(id="tts-1"),
            SimpleNamespace(id="omni-moderation-latest"),
            SimpleNamespace(id="gpt-image-1"),
        ]
        mock_client = MagicMock()
        mock_client.models.list = MagicMock(return_value=_async_list_iter(items))
        mock_client.close = AsyncMock()

        with patch("automation.agent.model_catalog.adapters.openai.AsyncOpenAI", return_value=mock_client):
            result = await OpenAIAdapter().list_models(_row())

        assert result == ["gpt-4o", "gpt-5.4"]

    async def test_empty_response_returns_empty_list(self):
        mock_client = MagicMock()
        mock_client.models.list = MagicMock(return_value=_async_list_iter([]))
        mock_client.close = AsyncMock()

        with patch("automation.agent.model_catalog.adapters.openai.AsyncOpenAI", return_value=mock_client):
            assert await OpenAIAdapter().list_models(_row()) == []

    async def test_sdk_error_wrapped_as_catalog_fetch_error(self):
        import openai

        class _Boom(openai.OpenAIError):
            pass

        mock_client = MagicMock()

        def _explode():
            raise _Boom("upstream is down")

        mock_client.models.list = MagicMock(side_effect=_explode)
        mock_client.close = AsyncMock()

        with (
            patch("automation.agent.model_catalog.adapters.openai.AsyncOpenAI", return_value=mock_client),
            pytest.raises(CatalogFetchError),
        ):
            await OpenAIAdapter().list_models(_row())

    async def test_missing_api_key_raises_before_sdk_call(self):
        with patch("automation.agent.model_catalog.adapters.openai.AsyncOpenAI") as ctor:
            with pytest.raises(MissingApiKeyError):  # noqa: SIM117
                await OpenAIAdapter().list_models(_row(api_key=None))
            ctor.assert_not_called()

    async def test_base_url_passed_to_sdk(self):
        items = []
        mock_client = MagicMock()
        mock_client.models.list = MagicMock(return_value=_async_list_iter(items))
        mock_client.close = AsyncMock()

        with patch("automation.agent.model_catalog.adapters.openai.AsyncOpenAI", return_value=mock_client) as ctor:
            await OpenAIAdapter().list_models(_row(base_url="https://proxy.example/v1"))
            _, kwargs = ctor.call_args
            assert kwargs["base_url"] == "https://proxy.example/v1"

    async def test_default_base_url_used_when_row_blank(self):
        items = []
        mock_client = MagicMock()
        mock_client.models.list = MagicMock(return_value=_async_list_iter(items))
        mock_client.close = AsyncMock()

        with patch("automation.agent.model_catalog.adapters.openai.AsyncOpenAI", return_value=mock_client) as ctor:
            await OpenAIAdapter().list_models(_row(base_url=""))
            _, kwargs = ctor.call_args
            assert kwargs["base_url"] == "https://api.openai.com/v1"


class TestAnthropicAdapter:
    async def test_returns_sorted_ids(self):
        items = [
            SimpleNamespace(id="claude-sonnet-4.6"),
            SimpleNamespace(id="claude-opus-4.6"),
            SimpleNamespace(id="claude-haiku-4.5"),
        ]
        mock_client = MagicMock()
        mock_client.models.list = MagicMock(return_value=_async_list_iter(items))
        mock_client.close = AsyncMock()

        with patch("automation.agent.model_catalog.adapters.anthropic.AsyncAnthropic", return_value=mock_client):
            result = await AnthropicAdapter().list_models(_row(slug="anthropic", provider_type=ProviderType.ANTHROPIC))

        assert result == ["claude-haiku-4.5", "claude-opus-4.6", "claude-sonnet-4.6"]

    async def test_no_filter_applied(self):
        """Anthropic ships chat models only; we don't drop anything."""
        items = [SimpleNamespace(id="claude-experimental-embedding")]
        mock_client = MagicMock()
        mock_client.models.list = MagicMock(return_value=_async_list_iter(items))
        mock_client.close = AsyncMock()

        with patch("automation.agent.model_catalog.adapters.anthropic.AsyncAnthropic", return_value=mock_client):
            result = await AnthropicAdapter().list_models(_row(slug="anthropic", provider_type=ProviderType.ANTHROPIC))

        assert result == ["claude-experimental-embedding"]

    async def test_sdk_error_wrapped(self):
        import anthropic

        class _Boom(anthropic.AnthropicError):
            pass

        mock_client = MagicMock()
        mock_client.models.list = MagicMock(side_effect=_Boom("nope"))
        mock_client.close = AsyncMock()

        with (
            patch("automation.agent.model_catalog.adapters.anthropic.AsyncAnthropic", return_value=mock_client),
            pytest.raises(CatalogFetchError),
        ):
            await AnthropicAdapter().list_models(_row(slug="anthropic", provider_type=ProviderType.ANTHROPIC))

    async def test_missing_api_key_raises(self):
        with patch("automation.agent.model_catalog.adapters.anthropic.AsyncAnthropic") as ctor:
            with pytest.raises(MissingApiKeyError):  # noqa: SIM117
                await AnthropicAdapter().list_models(
                    _row(slug="anthropic", provider_type=ProviderType.ANTHROPIC, api_key=None)
                )
            ctor.assert_not_called()

    async def test_default_base_url(self):
        mock_client = MagicMock()
        mock_client.models.list = MagicMock(return_value=_async_list_iter([]))
        mock_client.close = AsyncMock()

        with patch(
            "automation.agent.model_catalog.adapters.anthropic.AsyncAnthropic", return_value=mock_client
        ) as ctor:
            await AnthropicAdapter().list_models(_row(slug="anthropic", provider_type=ProviderType.ANTHROPIC))
            _, kwargs = ctor.call_args
            assert kwargs["base_url"] == "https://api.anthropic.com"
