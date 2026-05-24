"""Per-provider model catalog adapters.

Each adapter wraps the provider's official SDK ``models.list()`` endpoint and
returns sorted, chat-capable model ids. SDK exceptions are caught at the
adapter boundary and re-raised as :class:`CatalogFetchError`.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import anthropic
import openai
from google import genai as google_genai
from google.genai import errors as google_errors

from automation.agent.model_catalog.base import ModelCatalogAdapter
from automation.agent.model_catalog.exceptions import CatalogFetchError
from automation.agent.provider_clients import build_sdk_client_kwargs

if TYPE_CHECKING:
    from core.models import Provider

logger = logging.getLogger(__name__)


# Model-id patterns we exclude from chat-capable results.
# Conservative — when an id passes these filters but isn't truly a chat model
# the search input + free-text fallback in the picker still let users type
# their model name verbatim.
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


class OpenAIAdapter(ModelCatalogAdapter):
    _DEFAULT_BASE_URL = "https://api.openai.com/v1"

    async def list_models(self, row: Provider.Cached) -> list[str]:
        # build_sdk_client_kwargs raises MissingApiKeyError for missing keys — propagate.
        kw = build_sdk_client_kwargs(row)
        base_url = kw["base_url"] or self._DEFAULT_BASE_URL

        client_kwargs: dict = {"api_key": kw["api_key"], "base_url": base_url}
        if kw["default_headers"]:
            client_kwargs["default_headers"] = kw["default_headers"]
        if kw["http_client"] is not None:
            client_kwargs["http_client"] = kw["http_client"]

        ids: list[str] = []
        try:
            client = openai.AsyncOpenAI(**client_kwargs)
            try:
                async for model in client.models.list():
                    if _is_openai_chat_capable(model.id):
                        ids.append(model.id)
            finally:
                await client.close()
        except openai.OpenAIError as err:
            logger.warning("OpenAIAdapter failed for provider %r: %s", row.slug, err.__class__.__name__)
            raise CatalogFetchError(_safe_detail(err)) from err

        return sorted(ids)


class AnthropicAdapter(ModelCatalogAdapter):
    _DEFAULT_BASE_URL = "https://api.anthropic.com"

    async def list_models(self, row: Provider.Cached) -> list[str]:
        kw = build_sdk_client_kwargs(row)
        base_url = kw["base_url"] or self._DEFAULT_BASE_URL

        client_kwargs: dict = {"api_key": kw["api_key"], "base_url": base_url}
        if kw["default_headers"]:
            client_kwargs["default_headers"] = kw["default_headers"]
        if kw["http_client"] is not None:
            client_kwargs["http_client"] = kw["http_client"]

        ids: list[str] = []
        try:
            client = anthropic.AsyncAnthropic(**client_kwargs)
            try:
                async for model in client.models.list():
                    ids.append(model.id)
            finally:
                await client.close()
        except anthropic.AnthropicError as err:
            logger.warning("AnthropicAdapter failed for provider %r: %s", row.slug, err.__class__.__name__)
            raise CatalogFetchError(_safe_detail(err)) from err

        return sorted(ids)


class GoogleGenAIAdapter(ModelCatalogAdapter):
    async def list_models(self, row: Provider.Cached) -> list[str]:
        kw = build_sdk_client_kwargs(row)
        # google.genai.Client doesn't accept default_headers/http_client/base_url
        # the same way openai/anthropic do; we pass only api_key. Insecure-TLS
        # workaround for self-signed CAs isn't supported here — admins must mount
        # the CA into the container (same trade-off as inference; see base.py).
        if kw["http_client"] is not None:
            await kw["http_client"].aclose()
            logger.warning(
                "GoogleGenAIAdapter: verify_ssl=False ignored for provider %r"
                " (SDK has no http_client hook); mount the CA into the container.",
                row.slug,
            )

        ids: list[str] = []
        try:
            client = google_genai.Client(api_key=kw["api_key"])
            async for model in client.aio.models.list():
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

        return sorted(ids)
