"""Story 3.3 — the cached, batch-capable, fail-safe MR-state read-through (AC3, AC6, AC7).

Exercises the cache read-through (cold hits the client + ``cache.set``; warm skips the client),
webhook-style invalidation (``cache.delete`` via the centralized key), the fail-safe (any error →
``OPEN``, logged, cache not poisoned), and the batch entry point.
"""

import logging

from django.core.cache import cache

import pytest

from codebase.base import MergeRequestState
from codebase.mr_state import MR_STATE_CACHE_TTL, _mr_state_cache_key, get_merge_request_state, get_merge_request_states


@pytest.fixture(autouse=True)
def _clear_mr_state_cache():
    # LocMemCache is not cleared between tests; the mr_state keys would otherwise leak.
    cache.clear()
    yield
    cache.clear()


class TestCacheKey:
    def test_key_is_prefixed_and_scoped(self):
        assert _mr_state_cache_key("group/repo", 7) == "mr_state:group/repo:7"

    def test_ttl_is_short_window(self):
        assert MR_STATE_CACHE_TTL == 60


@pytest.mark.django_db
class TestCachedReadThrough:
    def test_cold_read_calls_client_and_caches_the_value(self, mock_repo_client):
        mock_repo_client.get_merge_request_state.return_value = MergeRequestState.MERGED

        result = get_merge_request_state("group/repo", 5)

        assert result == MergeRequestState.MERGED
        mock_repo_client.get_merge_request_state.assert_called_once_with("group/repo", merge_request_id=5)
        # The enum VALUE (a str) is cached, not the enum instance.
        assert cache.get(_mr_state_cache_key("group/repo", 5)) == "merged"

    def test_warm_read_skips_the_client(self, mock_repo_client):
        cache.set(_mr_state_cache_key("group/repo", 5), "closed", MR_STATE_CACHE_TTL)

        result = get_merge_request_state("group/repo", 5)

        assert result == MergeRequestState.CLOSED
        mock_repo_client.get_merge_request_state.assert_not_called()

    def test_warm_read_rehydrates_the_enum(self, mock_repo_client):
        cache.set(_mr_state_cache_key("group/repo", 5), "draft", MR_STATE_CACHE_TTL)

        result = get_merge_request_state("group/repo", 5)

        assert result is MergeRequestState.DRAFT

    def test_invalidation_deletes_the_key(self, mock_repo_client):
        mock_repo_client.get_merge_request_state.return_value = MergeRequestState.OPEN
        get_merge_request_state("group/repo", 5)
        assert cache.get(_mr_state_cache_key("group/repo", 5)) is not None

        # The webhook path deletes via the same centralized helper.
        cache.delete(_mr_state_cache_key("group/repo", 5))
        assert cache.get(_mr_state_cache_key("group/repo", 5)) is None

    def test_read_failure_returns_open_and_logs_without_poisoning_cache(self, mock_repo_client, caplog):
        mock_repo_client.get_merge_request_state.side_effect = RuntimeError("gitlab down")

        with caplog.at_level(logging.WARNING, logger="daiv.clients"):
            result = get_merge_request_state("group/repo", 5)

        assert result == MergeRequestState.OPEN  # fail-safe: keep the item visible (AC6)
        assert "keeping item visible" in caplog.text
        # A failed read must NOT persist a state — the next read retries.
        assert cache.get(_mr_state_cache_key("group/repo", 5)) is None


@pytest.mark.django_db
class TestBatchRead:
    def test_batch_returns_dict_keyed_by_iid(self, mock_repo_client):
        mock_repo_client.get_merge_request_state.side_effect = [MergeRequestState.MERGED, MergeRequestState.OPEN]

        result = get_merge_request_states("group/repo", [1, 2])

        assert result == {1: MergeRequestState.MERGED, 2: MergeRequestState.OPEN}

    def test_batch_serves_warm_items_without_a_client_call(self, mock_repo_client):
        cache.set(_mr_state_cache_key("group/repo", 1), "merged", MR_STATE_CACHE_TTL)
        mock_repo_client.get_merge_request_state.return_value = MergeRequestState.OPEN

        result = get_merge_request_states("group/repo", [1, 2])

        assert result == {1: MergeRequestState.MERGED, 2: MergeRequestState.OPEN}
        # Only the cold item (iid 2) hit the client.
        mock_repo_client.get_merge_request_state.assert_called_once_with("group/repo", merge_request_id=2)

    def test_batch_is_per_item_fail_safe(self, mock_repo_client):
        mock_repo_client.get_merge_request_state.side_effect = [MergeRequestState.MERGED, RuntimeError("boom")]

        result = get_merge_request_states("group/repo", [1, 2])

        assert result == {1: MergeRequestState.MERGED, 2: MergeRequestState.OPEN}
