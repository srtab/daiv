"""Maps :class:`core.models.ProviderType` values to adapter instances."""

from __future__ import annotations

from typing import TYPE_CHECKING

from automation.agent.model_catalog.adapters import (
    AnthropicAdapter,
    GoogleGenAIAdapter,
    OpenAIAdapter,
    OpenRouterAdapter,
)
from automation.agent.model_catalog.exceptions import UnsupportedProviderTypeError
from core.models import ProviderType

if TYPE_CHECKING:
    from automation.agent.model_catalog.base import ModelCatalogAdapter

_ADAPTERS: dict[ProviderType, ModelCatalogAdapter] = {
    ProviderType.OPENAI: OpenAIAdapter(),
    ProviderType.ANTHROPIC: AnthropicAdapter(),
    ProviderType.GOOGLE_GENAI: GoogleGenAIAdapter(),
    ProviderType.OPENROUTER: OpenRouterAdapter(),
}


def get_adapter(provider_type: ProviderType) -> ModelCatalogAdapter:
    """Return the adapter instance for a given provider type.

    Raises :class:`UnsupportedProviderTypeError` if no adapter is registered.
    """
    try:
        return _ADAPTERS[ProviderType(provider_type)]
    except (KeyError, ValueError) as err:
        raise UnsupportedProviderTypeError(f"No adapter registered for provider_type {provider_type!r}") from err
