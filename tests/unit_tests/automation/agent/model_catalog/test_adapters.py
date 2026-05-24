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

from automation.agent.model_catalog.adapters import (
    AnthropicAdapter,
    GoogleGenAIAdapter,
    OpenAIAdapter,
    OpenRouterAdapter,
)
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

    async def test_extra_headers_forwarded_to_sdk(self):
        row = _row()
        row = Provider.Cached(**{**row.__dict__, "extra_headers": {"X-Tenant": "acme"}})
        mock_client = MagicMock()
        mock_client.models.list = MagicMock(return_value=_async_list_iter([]))
        mock_client.close = AsyncMock()

        with patch("automation.agent.model_catalog.adapters.openai.AsyncOpenAI", return_value=mock_client) as ctor:
            await OpenAIAdapter().list_models(row)
            _, kwargs = ctor.call_args
            assert kwargs["default_headers"] == {"X-Tenant": "acme"}

    async def test_http_client_forwarded_when_verify_ssl_false(self):
        import httpx

        mock_client = MagicMock()
        mock_client.models.list = MagicMock(return_value=_async_list_iter([]))
        mock_client.close = AsyncMock()

        with patch("automation.agent.model_catalog.adapters.openai.AsyncOpenAI", return_value=mock_client) as ctor:
            await OpenAIAdapter().list_models(_row(verify_ssl=False))
            _, kwargs = ctor.call_args
            assert isinstance(kwargs["http_client"], httpx.AsyncClient)

    async def test_orphan_http_client_closed_when_sdk_ctor_raises(self):
        """If AsyncOpenAI(...) raises something other than OpenAIError, the externally
        supplied httpx.AsyncClient must still be closed — otherwise we leak the pool."""
        import httpx

        captured_clients: list = []
        orig_ctor = httpx.AsyncClient

        def _track_ctor(*args, **kwargs):
            client = orig_ctor(*args, **kwargs)
            captured_clients.append(client)
            return client

        with (
            patch("httpx.AsyncClient", side_effect=_track_ctor),
            patch(
                "automation.agent.model_catalog.adapters.openai.AsyncOpenAI",
                side_effect=TypeError("ctor blew up before binding client"),
            ),
            pytest.raises(TypeError),
        ):
            await OpenAIAdapter().list_models(_row(verify_ssl=False))

        assert len(captured_clients) == 1
        assert captured_clients[0].is_closed


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


class TestGoogleGenAIAdapter:
    async def test_filter_keeps_only_generate_content_models(self):
        items = [
            SimpleNamespace(name="models/gemini-2.5-pro", supported_actions=["generateContent"]),
            SimpleNamespace(name="models/gemini-2.5-flash", supported_actions=["generateContent", "countTokens"]),
            SimpleNamespace(name="models/text-embedding-004", supported_actions=["embedContent"]),
            SimpleNamespace(name="models/imagen-3.0", supported_actions=["generateImage"]),
            SimpleNamespace(name="models/gemini-no-actions", supported_actions=None),
        ]
        mock_aio = SimpleNamespace(models=MagicMock())
        mock_aio.models.list = AsyncMock(return_value=_async_list_iter(items))
        mock_client = SimpleNamespace(aio=mock_aio)

        with patch("automation.agent.model_catalog.adapters.google_genai.Client", return_value=mock_client):
            result = await GoogleGenAIAdapter().list_models(
                _row(slug="google_genai", provider_type=ProviderType.GOOGLE_GENAI)
            )

        assert result == ["gemini-2.5-flash", "gemini-2.5-pro"]

    async def test_strips_models_prefix(self):
        items = [SimpleNamespace(name="models/gemini-x", supported_actions=["generateContent"])]
        mock_aio = SimpleNamespace(models=MagicMock())
        mock_aio.models.list = AsyncMock(return_value=_async_list_iter(items))
        mock_client = SimpleNamespace(aio=mock_aio)

        with patch("automation.agent.model_catalog.adapters.google_genai.Client", return_value=mock_client):
            result = await GoogleGenAIAdapter().list_models(
                _row(slug="google_genai", provider_type=ProviderType.GOOGLE_GENAI)
            )

        assert result == ["gemini-x"]

    async def test_sdk_error_wrapped(self):
        from google.genai import errors as google_errors

        class _Boom(google_errors.APIError):
            def __init__(self):
                self.message = "nope"
                self.code = 500
                self.status = "INTERNAL"

        mock_aio = SimpleNamespace(models=MagicMock())
        mock_aio.models.list = AsyncMock(side_effect=_Boom())
        mock_client = SimpleNamespace(aio=mock_aio)

        with (
            patch("automation.agent.model_catalog.adapters.google_genai.Client", return_value=mock_client),
            pytest.raises(CatalogFetchError),
        ):
            await GoogleGenAIAdapter().list_models(_row(slug="google_genai", provider_type=ProviderType.GOOGLE_GENAI))

    async def test_missing_api_key_raises(self):
        with patch("automation.agent.model_catalog.adapters.google_genai.Client") as ctor:
            with pytest.raises(MissingApiKeyError):  # noqa: SIM117
                await GoogleGenAIAdapter().list_models(
                    _row(slug="google_genai", provider_type=ProviderType.GOOGLE_GENAI, api_key=None)
                )
            ctor.assert_not_called()

    async def test_closes_async_session_after_iteration(self):
        """``google.genai.Client.aio.aclose()`` must be awaited so the async httpx
        session does not leak. ``Client.close()`` only closes the sync session."""
        items = [SimpleNamespace(name="models/gemini-x", supported_actions=["generateContent"])]
        aclose_mock = AsyncMock()
        mock_aio = SimpleNamespace(models=MagicMock(), aclose=aclose_mock)
        mock_aio.models.list = AsyncMock(return_value=_async_list_iter(items))
        mock_client = SimpleNamespace(aio=mock_aio)

        with patch("automation.agent.model_catalog.adapters.google_genai.Client", return_value=mock_client):
            await GoogleGenAIAdapter().list_models(_row(slug="google_genai", provider_type=ProviderType.GOOGLE_GENAI))

        aclose_mock.assert_awaited_once()

    async def test_closes_unused_http_client_when_verify_ssl_false(self):
        """google.genai doesn't accept an http_client kwarg, so the AsyncClient
        built by build_sdk_client_kwargs must be aclose()d explicitly."""
        import httpx

        captured: list = []
        orig_ctor = httpx.AsyncClient

        def _track_ctor(*args, **kwargs):
            client = orig_ctor(*args, **kwargs)
            captured.append(client)
            return client

        aclose_mock = AsyncMock()
        mock_aio = SimpleNamespace(models=MagicMock(), aclose=aclose_mock)
        mock_aio.models.list = AsyncMock(return_value=_async_list_iter([]))
        mock_client = SimpleNamespace(aio=mock_aio)

        with (
            patch("httpx.AsyncClient", side_effect=_track_ctor),
            patch("automation.agent.model_catalog.adapters.google_genai.Client", return_value=mock_client),
        ):
            await GoogleGenAIAdapter().list_models(
                _row(slug="google_genai", provider_type=ProviderType.GOOGLE_GENAI, verify_ssl=False)
            )

        assert len(captured) == 1
        assert captured[0].is_closed


class TestOpenRouterAdapter:
    async def test_returns_sorted_filtered_ids(self):
        items = [
            SimpleNamespace(id="anthropic/claude-haiku-4.5"),
            SimpleNamespace(id="anthropic/claude-opus-4.6"),
            SimpleNamespace(id="openai/text-embedding-3-large"),
            SimpleNamespace(id="openai/whisper-1"),
            SimpleNamespace(id="black-forest-labs/flux-image"),
            SimpleNamespace(id="z-ai/glm-5"),
        ]
        mock_client = MagicMock()
        mock_client.models.list = MagicMock(return_value=_async_list_iter(items))
        mock_client.close = AsyncMock()

        with patch("automation.agent.model_catalog.adapters.openai.AsyncOpenAI", return_value=mock_client):
            result = await OpenRouterAdapter().list_models(
                _row(slug="openrouter", provider_type=ProviderType.OPENROUTER)
            )

        assert result == ["anthropic/claude-haiku-4.5", "anthropic/claude-opus-4.6", "z-ai/glm-5"]

    async def test_default_base_url_is_openrouter(self):
        mock_client = MagicMock()
        mock_client.models.list = MagicMock(return_value=_async_list_iter([]))
        mock_client.close = AsyncMock()

        with patch("automation.agent.model_catalog.adapters.openai.AsyncOpenAI", return_value=mock_client) as ctor:
            await OpenRouterAdapter().list_models(_row(slug="openrouter", provider_type=ProviderType.OPENROUTER))
            _, kwargs = ctor.call_args
            assert kwargs["base_url"] == "https://openrouter.ai/api/v1"

    async def test_row_base_url_overrides_default(self):
        mock_client = MagicMock()
        mock_client.models.list = MagicMock(return_value=_async_list_iter([]))
        mock_client.close = AsyncMock()

        with patch("automation.agent.model_catalog.adapters.openai.AsyncOpenAI", return_value=mock_client) as ctor:
            await OpenRouterAdapter().list_models(
                _row(
                    slug="openrouter",
                    provider_type=ProviderType.OPENROUTER,
                    base_url="https://proxy.example/openrouter",
                )
            )
            _, kwargs = ctor.call_args
            assert kwargs["base_url"] == "https://proxy.example/openrouter"
