"""Maps :class:`core.models.ProviderType` values to adapter instances.

Entries are added one per adapter as each is implemented. See Tasks 4-7.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from automation.agent.model_catalog.adapters import OpenAIAdapter
from automation.agent.model_catalog.exceptions import UnsupportedProviderTypeError
from core.models import ProviderType

if TYPE_CHECKING:
    from automation.agent.model_catalog.base import ModelCatalogAdapter


class _NotImplementedAdapter:
    """Placeholder for provider types whose adapters are not yet implemented."""

    def __init__(self, provider_type: ProviderType) -> None:
        self._provider_type = provider_type

    async def list_models(self, row):  # noqa: ARG002
        raise UnsupportedProviderTypeError(f"Adapter for provider_type {self._provider_type!r} not implemented yet")


_ADAPTERS: dict[ProviderType, ModelCatalogAdapter] = {
    ProviderType.OPENAI: OpenAIAdapter(),
    ProviderType.ANTHROPIC: _NotImplementedAdapter(ProviderType.ANTHROPIC),  # type: ignore[dict-item]
    ProviderType.GOOGLE_GENAI: _NotImplementedAdapter(ProviderType.GOOGLE_GENAI),  # type: ignore[dict-item]
    ProviderType.OPENROUTER: _NotImplementedAdapter(ProviderType.OPENROUTER),  # type: ignore[dict-item]
}


def get_adapter(provider_type: ProviderType) -> ModelCatalogAdapter:
    """Return the adapter instance for a given provider type.

    Raises :class:`UnsupportedProviderTypeError` if no adapter is registered.
    """
    try:
        return _ADAPTERS[ProviderType(provider_type)]
    except (KeyError, ValueError) as err:
        raise UnsupportedProviderTypeError(f"No adapter registered for provider_type {provider_type!r}") from err
