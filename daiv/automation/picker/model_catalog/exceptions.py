"""Exception hierarchy for the model catalog module.

Adapter and service errors derive from :class:`ModelCatalogError`, except
:class:`~automation.agent.provider_clients.MissingApiKeyError`, which
``build_sdk_client_kwargs`` raises for the chat-model builder too. SDK-specific exceptions (``openai.OpenAIError`` etc.)
are caught at the adapter boundary and re-raised as ``CatalogFetchError`` with
a short, user-safe ``detail`` string — they never leak out of this module.
"""

from __future__ import annotations


class ModelCatalogError(Exception):
    """Base class for all catalog-related errors."""


class UnsupportedProviderTypeError(ModelCatalogError):
    """Provider's ``provider_type`` has no registered adapter."""


class CatalogFetchError(ModelCatalogError):
    """Generic adapter failure (HTTP/network/parse/timeout).

    ``detail`` is a short, safe string suitable for user display
    (no API keys, no full response bodies, no stack traces).
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail
