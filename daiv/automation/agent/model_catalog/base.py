"""ABC for model catalog adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.models import Provider


class ModelCatalogAdapter(ABC):
    """Provider-specific model lister.

    Adapter contract:
      - Return chat-capable model names only (no ``slug:`` prefix; the picker concatenates).
      - Output is alphabetically sorted; the frontend does not re-sort.
      - Empty list is a valid successful return.
      - Missing API key → :class:`MissingApiKeyError` (raised by ``build_sdk_client_kwargs``).
      - SDK exceptions → wrapped as :class:`CatalogFetchError` with a safe ``detail``.
      - The adapter owns the lifecycle of any ``httpx.AsyncClient`` passed in via
        ``build_sdk_client_kwargs`` and closes it in a ``finally`` block.
    """

    @abstractmethod
    async def list_models(self, row: Provider.Cached) -> list[str]: ...
