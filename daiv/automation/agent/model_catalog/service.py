"""Cache-backed concurrent fetch over per-provider catalog adapters.
Errors are returned to the caller but never cached so the next open retries."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.core.cache import cache

from automation.agent.model_catalog.exceptions import (
    CatalogFetchError,
    MissingApiKeyError,
    UnsupportedProviderTypeError,
)
from automation.agent.model_catalog.registry import get_adapter

if TYPE_CHECKING:
    from core.models import Provider

logger = logging.getLogger(__name__)

MODEL_CATALOG_CACHE_KEY_FMT = "agent_picker:models:{slug}:v1"
MODEL_CATALOG_CACHE_TTL = 60 * 15  # 15 minutes
MODEL_CATALOG_FETCH_TIMEOUT = 4.0  # seconds, per-provider
MODEL_CATALOG_TOTAL_TIMEOUT = 6.0  # seconds, whole gather
MODEL_CATALOG_CANCEL_GRACE = 0.5  # seconds for cancelled tasks to clean up


@dataclass(frozen=True)
class CatalogEntry:
    """Per-provider catalog snapshot returned to the view."""

    models: list[str]
    error: str | None  # None on success (including empty-but-valid responses)


_ERROR_MAP: dict[type[Exception], str] = {
    MissingApiKeyError: "API key not configured",
    UnsupportedProviderTypeError: "unsupported provider type",
    asyncio.TimeoutError: "Request timed out",
}


def _safe_error_string(exc: Exception) -> str:
    """Map an exception to a short, user-safe error string.

    Falls back to ``CatalogFetchError.detail`` (which the adapter has already
    shortened) or a generic catch-all.
    """
    for exc_type, message in _ERROR_MAP.items():
        if isinstance(exc, exc_type):
            return message
    if isinstance(exc, CatalogFetchError):
        return exc.detail
    return "Unexpected error"


async def _fetch_one(row: Provider.Cached) -> CatalogEntry:
    """Fetch one provider's catalog. Reads from cache; misses go through the adapter."""
    cache_key = MODEL_CATALOG_CACHE_KEY_FMT.format(slug=row.slug)
    cached: CatalogEntry | None = cache.get(cache_key)
    if cached is not None:
        logger.debug("model catalog cache hit for %r", row.slug)
        return cached

    try:
        adapter = get_adapter(row.provider_type)
        models = await asyncio.wait_for(adapter.list_models(row), timeout=MODEL_CATALOG_FETCH_TIMEOUT)
    except TimeoutError as err:
        logger.warning("model catalog timeout for %r", row.slug)
        return CatalogEntry(models=[], error=_safe_error_string(err))
    except (MissingApiKeyError, UnsupportedProviderTypeError, CatalogFetchError) as err:
        logger.warning("model catalog failure for %r (%s)", row.slug, err.__class__.__name__)
        return CatalogEntry(models=[], error=_safe_error_string(err))
    except Exception as err:  # noqa: BLE001
        logger.exception("unexpected model catalog failure for %r", row.slug)
        return CatalogEntry(models=[], error=_safe_error_string(err))

    entry = CatalogEntry(models=models, error=None)
    cache.set(cache_key, entry, MODEL_CATALOG_CACHE_TTL)
    logger.info("model catalog miss for %r — fetched %d models", row.slug, len(models))
    return entry


async def fetch_catalog(rows: list[Provider.Cached]) -> dict[str, CatalogEntry]:
    """Bounded by ``MODEL_CATALOG_TOTAL_TIMEOUT``; rows still in flight when the
    total budget elapses get ``error='Request timed out'``."""
    if not rows:
        return {}

    tasks = {row.slug: asyncio.create_task(_fetch_one(row)) for row in rows}
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(
            asyncio.gather(*tasks.values(), return_exceptions=True), timeout=MODEL_CATALOG_TOTAL_TIMEOUT
        )

    out: dict[str, CatalogEntry] = {}
    pending: list[asyncio.Task] = []
    for slug, task in tasks.items():
        if task.done() and not task.cancelled():
            try:
                out[slug] = task.result()
            except Exception as err:  # noqa: BLE001
                # _fetch_one swallows its own errors, so this only fires on
                # unforeseen task-level failures (e.g. cancellation race).
                logger.exception("task-level catalog failure for %r", slug)
                out[slug] = CatalogEntry(models=[], error=_safe_error_string(err))
        else:
            task.cancel()
            pending.append(task)
            out[slug] = CatalogEntry(models=[], error="Request timed out")

    # Give cancelled tasks a brief window to run their ``finally`` blocks so
    # adapter http_client / SDK sessions are closed properly.
    if pending:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=MODEL_CATALOG_CANCEL_GRACE)

    return out
