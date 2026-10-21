"""Maps :class:`core.models.ProviderType` values to adapter instances.

The adapters, and the provider SDKs they import, load on the first lookup: the agent-models route imports this
module when the URLconf loads.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING

from automation.picker.model_catalog.exceptions import UnsupportedProviderTypeError
from core.models import ProviderType

if TYPE_CHECKING:
    from automation.picker.model_catalog.base import ModelCatalogAdapter


@cache
def _adapters() -> dict[ProviderType, ModelCatalogAdapter]:
    from automation.picker.model_catalog.adapters import (
        AnthropicAdapter,
        GoogleGenAIAdapter,
        OpenAIAdapter,
        OpenRouterAdapter,
    )

    return {
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
        return _adapters()[ProviderType(provider_type)]
    except (KeyError, ValueError) as err:
        raise UnsupportedProviderTypeError(f"No adapter registered for provider_type {provider_type!r}") from err
