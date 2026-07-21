from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.core.cache import cache

from codebase.base import MergeRequestState

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger("daiv.clients")

MR_STATE_CACHE_KEY_PREFIX = "mr_state:"
# Short TTL (W2 render-latency/cache decision): a refresh reflects reality within this window.
# Eager webhook invalidation (codebase/clients/{gitlab,github}/api/callbacks.py) is the primary
# freshness path; this TTL is the fallback for a close-without-merge or a missed webhook.
MR_STATE_CACHE_TTL = 60  # seconds


def _mr_state_cache_key(repo_id: str, iid: int) -> str:
    """Build the cache key for one MR's live state.

    The single source of the key (AC7), used by BOTH the read-through below and the webhook
    invalidation ``cache.delete`` — a divergent key would leave a merged MR cached-stale until the
    TTL, so the read and the invalidation must never drift.
    """
    return f"{MR_STATE_CACHE_KEY_PREFIX}{repo_id}:{iid}"


def get_merge_request_state(repo_id: str, iid: int) -> MergeRequestState:
    """Cached read-through for one MR's live lifecycle state (AC3, AC6, AC7).

    Cache-first, mirroring the ``codebase/repo_config.py`` idiom: a warm key returns the rehydrated
    enum with NO client call; a cold key calls the provider and caches the enum ``.value`` (a
    ``str``) for :data:`MR_STATE_CACHE_TTL`. This wrapper OWNS the fail-safe: any exception, timeout,
    or unconfigured client resolves to :attr:`MergeRequestState.OPEN` (logged at ``warning``) so a
    read failure keeps the item visible (NFR1) and never raises up into a render path.
    """
    key = _mr_state_cache_key(repo_id, iid)
    try:
        if (cached := cache.get(key)) is not None:
            return MergeRequestState(cached)

        # Lazy import: keep this module importable without pulling the whole client graph, and
        # let the mocked factory take over in tests.
        from codebase.clients import RepoClient

        state = RepoClient.create_instance().get_merge_request_state(repo_id, merge_request_id=iid)
        cache.set(key, state.value, MR_STATE_CACHE_TTL)
        return state
    except Exception:
        logger.warning("Live MR-state read failed for %s!%s; keeping item visible", repo_id, iid, exc_info=True)
        return MergeRequestState.OPEN


def get_merge_request_states(repo_id: str, iids: Iterable[int]) -> dict[int, MergeRequestState]:
    """Batch entry point: resolve many MRs' live states in one call (AC7).

    Loops the per-item cached read (warm items cost nothing), so Epic 4's Needs-me Queue can
    reconcile a list of MRs without N cold sequential round-trips. Each item is independently
    fail-safe (a per-item failure resolves to ``OPEN``).
    """
    return {iid: get_merge_request_state(repo_id, iid) for iid in iids}
