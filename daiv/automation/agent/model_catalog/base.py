"""ABC for model catalog adapters.

Implementations live in :mod:`automation.agent.model_catalog.adapters`. Each
adapter wraps a provider SDK's ``models.list()`` call, filters to chat-capable
models, and returns alphabetically-sorted model identifiers (without provider
slug prefix — the picker concatenates the slug).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.models import Provider


class ModelCatalogAdapter(ABC):
    """Provider-specific model lister.

    Common contract for all adapters:
      - Return type is the model name only (no ``slug:`` prefix).
      - Output is alphabetically sorted; frontend does not re-sort.
      - Empty list is a valid successful return.
      - ``httpx.AsyncClient`` lifecycle is managed via ``async with``.

    Errors:
      - Missing API key → :class:`MissingApiKeyError` (handled by ``build_sdk_client_kwargs``).
      - SDK exceptions → wrapped as :class:`CatalogFetchError` with a safe ``detail``.
    """

    @abstractmethod
    async def list_models(self, row: Provider.Cached) -> list[str]:
        """Return chat-capable model identifiers for the given provider row,
        alphabetically sorted. Raises a :class:`ModelCatalogError` subclass on failure."""
