"""Exception hierarchy for the model catalog module.

All exceptions raised out of adapters or the service derive from
:class:`ModelCatalogError`. SDK-specific exceptions (``openai.OpenAIError`` etc.)
are caught at the adapter boundary and re-raised as ``CatalogFetchError`` with
a short, user-safe ``detail`` string — they never leak out of this module.
"""

from __future__ import annotations


class ModelCatalogError(Exception):
    """Base class for all catalog-related errors."""


class MissingApiKeyError(ModelCatalogError):
    """Provider row has no API key configured."""


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
