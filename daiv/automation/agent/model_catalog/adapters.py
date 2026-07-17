"""Per-provider model catalog adapters. SDK exceptions are wrapped as
:class:`CatalogFetchError` at the adapter boundary."""

from __future__ import annotations

import contextlib
import logging
import re
from typing import TYPE_CHECKING

import anthropic
import openai
from google import genai as google_genai
from google.genai import errors as google_errors

from automation.agent.chat_models import OPENROUTER_BASE_URL
from automation.agent.model_catalog.base import ModelCatalogAdapter
from automation.agent.model_catalog.exceptions import CatalogFetchError
from automation.agent.provider_clients import build_sdk_client_kwargs

if TYPE_CHECKING:
    from core.models import Provider

logger = logging.getLogger(__name__)


# Conservative — when an id passes these filters but isn't truly a chat model,
# the picker's free-text input still lets users type the model name verbatim.
_OPENAI_NON_CHAT_PATTERNS = (
    re.compile(r"embedding", re.IGNORECASE),
    re.compile(r"^whisper-", re.IGNORECASE),
    re.compile(r"^tts-", re.IGNORECASE),
    re.compile(r"^dall-e-", re.IGNORECASE),
    re.compile(r"moderation", re.IGNORECASE),
    re.compile(r"^text-davinci-", re.IGNORECASE),
    re.compile(r"^gpt-image-", re.IGNORECASE),
    re.compile(r"-search-", re.IGNORECASE),
)


def _is_openai_chat_capable(model_id: str) -> bool:
    return not any(pat.search(model_id) for pat in _OPENAI_NON_CHAT_PATTERNS)


def _safe_detail(exc: Exception, max_len: int = 120) -> str:
    """Return a short, user-safe error string. Never returns secrets or response bodies."""
    text = str(exc).strip() or exc.__class__.__name__
    return text[:max_len]


async def _aclose_orphan(http_client) -> None:
    """Close an httpx.AsyncClient that the SDK never took ownership of.

    The SDK's ``client.close()`` closes the httpx client it was passed — but only
    if the SDK constructor returned successfully. If construction raises before
    that, we own the orphan and must aclose it ourselves to avoid a connection-
    pool leak.
    """
    if http_client is None:
        return
    with contextlib.suppress(Exception):
        await http_client.aclose()


class OpenAIAdapter(ModelCatalogAdapter):
    _DEFAULT_BASE_URL = "https://api.openai.com/v1"

    async def list_models(self, row: Provider.Cached) -> list[str]:
        # build_sdk_client_kwargs raises MissingApiKeyError for missing keys — propagate.
        kw = build_sdk_client_kwargs(row)
        base_url = kw["base_url"] or self._DEFAULT_BASE_URL
        http_client = kw["http_client"]

        client_kwargs: dict = {"api_key": kw["api_key"], "base_url": base_url}
        if kw["default_headers"]:
            client_kwargs["default_headers"] = kw["default_headers"]
        if http_client is not None:
            client_kwargs["http_client"] = http_client

        ids: list[str] = []
        client = None
        try:
            client = openai.AsyncOpenAI(**client_kwargs)
            async for model in client.models.list():
                if _is_openai_chat_capable(model.id):
                    ids.append(model.id)
        except openai.OpenAIError as err:
            logger.warning("OpenAIAdapter failed for provider %r: %s", row.slug, err.__class__.__name__)
            raise CatalogFetchError(_safe_detail(err)) from err
        finally:
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.close()
            else:
                # SDK ctor never bound `client` — close the orphan ourselves.
                await _aclose_orphan(http_client)

        return sorted(ids)


class AnthropicAdapter(ModelCatalogAdapter):
    _DEFAULT_BASE_URL = "https://api.anthropic.com"

    async def list_models(self, row: Provider.Cached) -> list[str]:
        kw = build_sdk_client_kwargs(row)
        base_url = kw["base_url"] or self._DEFAULT_BASE_URL
        http_client = kw["http_client"]

        client_kwargs: dict = {"api_key": kw["api_key"], "base_url": base_url}
        if kw["default_headers"]:
            client_kwargs["default_headers"] = kw["default_headers"]
        if http_client is not None:
            client_kwargs["http_client"] = http_client

        ids: list[str] = []
        client = None
        try:
            client = anthropic.AsyncAnthropic(**client_kwargs)
            async for model in client.models.list():
                ids.append(model.id)
        except anthropic.AnthropicError as err:
            logger.warning("AnthropicAdapter failed for provider %r: %s", row.slug, err.__class__.__name__)
            raise CatalogFetchError(_safe_detail(err)) from err
        finally:
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.close()
            else:
                await _aclose_orphan(http_client)

        return sorted(ids)


class GoogleGenAIAdapter(ModelCatalogAdapter):
    async def list_models(self, row: Provider.Cached) -> list[str]:
        kw = build_sdk_client_kwargs(row)
        # google.genai.Client doesn't expose default_headers/http_client/base_url
        # the same way openai/anthropic do — we pass only api_key. The verify_ssl=False
        # escape hatch isn't supported here; admins must mount the CA into the container.
        if kw["http_client"] is not None:
            await _aclose_orphan(kw["http_client"])
            logger.warning(
                "GoogleGenAIAdapter: verify_ssl=False ignored for provider %r"
                " (SDK has no http_client hook); mount the CA into the container.",
                row.slug,
            )

        ids: list[str] = []
        client = None
        try:
            client = google_genai.Client(api_key=kw["api_key"])
            # ``AsyncModels.list()`` is a coroutine that resolves to an AsyncPager;
            # await first, then iterate.
            pager = await client.aio.models.list()
            async for model in pager:
                actions = getattr(model, "supported_actions", None) or []
                if "generateContent" not in actions:
                    continue
                name = model.name or ""
                if name.startswith("models/"):
                    name = name[len("models/") :]
                if name:
                    ids.append(name)
        except google_errors.APIError as err:
            logger.warning("GoogleGenAIAdapter failed for provider %r: %s", row.slug, err.__class__.__name__)
            raise CatalogFetchError(_safe_detail(err)) from err
        finally:
            # Client.close() only closes the sync session per the SDK docs;
            # Client.aio.aclose() releases the async httpx session we just used.
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.aio.aclose()

        return sorted(ids)


class OpenRouterAdapter(ModelCatalogAdapter):
    """OpenAI-compatible over the wire — reuse the openai SDK with a different base URL."""

    _DEFAULT_BASE_URL = OPENROUTER_BASE_URL

    # OpenRouter's ``/models`` supports two capability filters we care about:
    #   - ``output_modalities=text`` — drops embeddings, image-generation, TTS,
    #     etc. (defaults to ``text`` upstream, but pass explicitly so behaviour
    #     survives any future default change).
    #   - ``supported_parameters=tools`` — drops models without tool-calling
    #     support. The deep agent's middleware stack (Skills, Sandbox, MCP,
    #     WebSearch, …) is tool-driven, so a non-tools model is unusable here.
    # Reasoning / structured_outputs are intentionally NOT filtered: reasoning
    # is opt-in via thinking level, and structured outputs are only used by
    # ``diff_to_metadata`` / ``titling``, which read admin-configured models
    # instead of the picker. The picker's free-text input is the safety valve
    # for the rare case the server filter is too aggressive.
    _MODELS_QUERY = {"output_modalities": "text", "supported_parameters": "tools"}

    async def list_models(self, row: Provider.Cached) -> list[str]:
        kw = build_sdk_client_kwargs(row)
        base_url = kw["base_url"] or self._DEFAULT_BASE_URL
        http_client = kw["http_client"]

        client_kwargs: dict = {"api_key": kw["api_key"], "base_url": base_url}
        if kw["default_headers"]:
            client_kwargs["default_headers"] = kw["default_headers"]
        if http_client is not None:
            client_kwargs["http_client"] = http_client

        ids: list[str] = []
        client = None
        try:
            client = openai.AsyncOpenAI(**client_kwargs)
            async for model in client.models.list(extra_query=self._MODELS_QUERY):
                ids.append(model.id)
        except openai.OpenAIError as err:
            logger.warning("OpenRouterAdapter failed for provider %r: %s", row.slug, err.__class__.__name__)
            raise CatalogFetchError(_safe_detail(err)) from err
        finally:
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.close()
            else:
                await _aclose_orphan(http_client)

        return sorted(ids)
