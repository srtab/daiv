from datetime import datetime, timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.utils import timezone

import pytest
from allauth.socialaccount.models import SocialAccount

from codebase.authorization import (
    RepositoryAccessDenied,
    assert_can_run,
    can_view,
    filter_viewable,
    get_access_level,
    search_viewable_repositories,
    viewable_repo_ids,
)
from codebase.base import GitPlatform, RepoAccessLevel, Repository
from codebase.models import RepositoryAccess, RepositoryAccessSyncState, RepositoryCatalog


def _repo(slug: str) -> Repository:
    return Repository(
        pk=abs(hash(slug)) % (2**31),
        slug=slug,
        name=slug.split("/")[-1],
        clone_url=f"https://example/{slug}.git",
        html_url=f"https://example/{slug}",
        default_branch="main",
        git_platform=GitPlatform.GITLAB,
        topics=[],
    )


@pytest.fixture
def fresh_sync(db):
    """A live scheduler: a sync started and succeeded recently (suppresses the backstop enqueue)."""
    now = timezone.now()
    return RepositoryAccessSyncState.objects.create(
        pk=RepositoryAccessSyncState.SINGLETON_PK,
        last_started_at=now,
        last_success_at=now,
        status=RepositoryAccessSyncState.Status.OK,
    )


@pytest.fixture
def linked_member(member_user):
    SocialAccount.objects.create(user=member_user, provider="gitlab", uid="101")
    return member_user


def _grant(uid: str, repo_id: str, level: RepoAccessLevel, synced_at: datetime | None = None):
    RepositoryAccess.objects.create(
        provider="gitlab",
        uid=uid,
        username="u",
        repo_id=repo_id,
        access_level=level,
        synced_at=synced_at or timezone.now(),
    )


def _catalog(slug: str, *, name: str | None = None, topics: list[str] | None = None, synced_at: datetime | None = None):
    return RepositoryCatalog.objects.create(
        provider="gitlab",
        slug=slug,
        name=name if name is not None else slug.split("/")[-1],
        default_branch="main",
        html_url=f"https://example/{slug}",
        topics=topics or [],
        synced_at=synced_at or timezone.now(),
    )


@pytest.fixture(autouse=True)
def _no_backstop_enqueue():
    # Clear the once-a-minute backstop-probe cache marker so each test evaluates the probe
    # deterministically rather than inheriting a marker set by a prior test.
    cache.delete("repo-access:backstop-probe")
    with patch("codebase.authorization._enqueue_sync") as m:
        yield m


class TestGetAccessLevel:
    def test_admin_bypasses_everything(self, admin_user, db):
        assert get_access_level(admin_user, "any/repo") == RepoAccessLevel.WRITE

    def test_member_without_social_account_denied(self, member_user, fresh_sync):
        assert get_access_level(member_user, "a/b") is None

    def test_wrong_provider_social_account_denied(self, member_user, fresh_sync):
        # A user linked to a different platform than the configured CLIENT resolves to uid=None.
        SocialAccount.objects.create(user=member_user, provider="github", uid="101")
        _grant("101", "a/b", RepoAccessLevel.WRITE)
        assert get_access_level(member_user, "a/b") is None

    def test_member_with_read_row(self, linked_member, fresh_sync):
        _grant("101", "a/b", RepoAccessLevel.READ)
        assert get_access_level(linked_member, "a/b") == RepoAccessLevel.READ
        assert can_view(linked_member, "a/b") is True
        assert can_view(linked_member, "other/repo") is False

    def test_no_rows_denies_and_enqueues_backstop(self, linked_member, db, _no_backstop_enqueue):
        # Cold start: no synced rows and no sync ever started -> deny and trigger a backstop sync.
        assert get_access_level(linked_member, "a/b") is None
        _no_backstop_enqueue.assert_called()

    def test_stale_row_beyond_hard_ttl_denies(self, linked_member, fresh_sync):
        _grant("101", "a/b", RepoAccessLevel.WRITE, synced_at=timezone.now() - timedelta(hours=25))
        assert get_access_level(linked_member, "a/b") is None

    def test_fresh_row_serves_regardless_of_global_state(self, linked_member, db):
        # Freshness is per-row: a row synced within the TTL grants access even with no sync-state row.
        _grant("101", "a/b", RepoAccessLevel.WRITE, synced_at=timezone.now() - timedelta(hours=2))
        assert get_access_level(linked_member, "a/b") == RepoAccessLevel.WRITE

    def test_per_repo_freshness_is_independent(self, linked_member, fresh_sync):
        # A stale repo is denied while a sibling fresh repo is still granted (fix for the
        # single-failing-repo global-outage regression).
        _grant("101", "fresh/repo", RepoAccessLevel.WRITE)
        _grant("101", "stale/repo", RepoAccessLevel.WRITE, synced_at=timezone.now() - timedelta(hours=30))
        assert get_access_level(linked_member, "fresh/repo") == RepoAccessLevel.WRITE
        assert get_access_level(linked_member, "stale/repo") is None

    def test_stale_started_at_enqueues_backstop(self, linked_member, db, _no_backstop_enqueue):
        # A dead scheduler (no run started within the backstop window) triggers an enqueue.
        RepositoryAccessSyncState.objects.create(
            pk=RepositoryAccessSyncState.SINGLETON_PK,
            last_started_at=timezone.now() - timedelta(hours=2),
            last_success_at=timezone.now() - timedelta(hours=2),
            status=RepositoryAccessSyncState.Status.OK,
        )
        _grant("101", "a/b", RepoAccessLevel.WRITE, synced_at=timezone.now() - timedelta(hours=2))
        assert get_access_level(linked_member, "a/b") == RepoAccessLevel.WRITE
        _no_backstop_enqueue.assert_called()

    def test_live_scheduler_does_not_enqueue(self, linked_member, fresh_sync, _no_backstop_enqueue):
        get_access_level(linked_member, "a/b")
        _no_backstop_enqueue.assert_not_called()

    def test_backstop_keys_on_started_not_success(self, linked_member, db, _no_backstop_enqueue):
        # The discriminating case: a live scheduler with a persistently-failing repo has a fresh
        # last_started_at but a stale last_success_at. The backstop must NOT fire (it keys on
        # last_started_at); a regression back to keying on last_success_at would flood the queue.
        RepositoryAccessSyncState.objects.create(
            pk=RepositoryAccessSyncState.SINGLETON_PK,
            last_started_at=timezone.now(),
            last_success_at=timezone.now() - timedelta(hours=5),
            status=RepositoryAccessSyncState.Status.FAILED,
        )
        get_access_level(linked_member, "a/b")
        _no_backstop_enqueue.assert_not_called()


class TestAssertCanRun:
    def test_write_rows_pass(self, linked_member, fresh_sync):
        _grant("101", "a/b", RepoAccessLevel.WRITE)
        assert_can_run(linked_member, ["a/b"])  # no raise

    def test_read_only_repo_denied_with_ids(self, linked_member, fresh_sync):
        _grant("101", "a/b", RepoAccessLevel.WRITE)
        _grant("101", "c/d", RepoAccessLevel.READ)
        with pytest.raises(RepositoryAccessDenied) as exc:
            assert_can_run(linked_member, ["a/b", "c/d", "e/f"])
        assert exc.value.repo_ids == ["c/d", "e/f"]

    def test_admin_passes_without_rows(self, admin_user, db):
        assert_can_run(admin_user, ["any/repo"])

    def test_no_rows_denies(self, linked_member, db, _no_backstop_enqueue):
        with pytest.raises(RepositoryAccessDenied) as exc:
            assert_can_run(linked_member, ["a/b"])
        assert exc.value.repo_ids == ["a/b"]

    def test_no_social_account_denies(self, member_user, fresh_sync):
        # A user with a DAIV account but no linked platform identity resolves to uid=None and
        # must get zero WRITE access — even if a stray row exists under some uid.
        _grant("101", "a/b", RepoAccessLevel.WRITE)
        with pytest.raises(RepositoryAccessDenied) as exc:
            assert_can_run(member_user, ["a/b"])
        assert exc.value.repo_ids == ["a/b"]

    def test_stale_row_beyond_hard_ttl_denies(self, linked_member, fresh_sync):
        _grant("101", "a/b", RepoAccessLevel.WRITE, synced_at=timezone.now() - timedelta(hours=25))
        with pytest.raises(RepositoryAccessDenied) as exc:
            assert_can_run(linked_member, ["a/b"])
        assert exc.value.repo_ids == ["a/b"]

    def test_fresh_repo_passes_while_stale_sibling_is_denied(self, linked_member, fresh_sync):
        # Per-repo freshness independence: a stale repo must not cause collateral denial of a
        # fresh sibling passed in the same call — only the stale one lands in repo_ids.
        _grant("101", "fresh/repo", RepoAccessLevel.WRITE)
        _grant("101", "stale/repo", RepoAccessLevel.WRITE, synced_at=timezone.now() - timedelta(hours=30))
        with pytest.raises(RepositoryAccessDenied) as exc:
            assert_can_run(linked_member, ["fresh/repo", "stale/repo"])
        assert exc.value.repo_ids == ["stale/repo"]


class TestViewableFilters:
    def test_viewable_repo_ids_subset(self, linked_member, fresh_sync):
        _grant("101", "a/b", RepoAccessLevel.READ)
        assert viewable_repo_ids(linked_member, ["a/b", "x/y"]) == {"a/b"}

    def test_stale_rows_excluded(self, linked_member, fresh_sync):
        _grant("101", "a/b", RepoAccessLevel.READ)
        _grant("101", "c/d", RepoAccessLevel.READ, synced_at=timezone.now() - timedelta(hours=25))
        assert viewable_repo_ids(linked_member, ["a/b", "c/d"]) == {"a/b"}

    def test_filter_viewable_preserves_order(self, linked_member, fresh_sync):
        _grant("101", "c/d", RepoAccessLevel.WRITE)
        _grant("101", "a/b", RepoAccessLevel.READ)
        repos = [_repo("a/b"), _repo("x/y"), _repo("c/d")]
        assert [r.slug for r in filter_viewable(linked_member, repos)] == ["a/b", "c/d"]

    def test_admin_sees_all(self, admin_user, db):
        repos = [_repo("a/b"), _repo("x/y")]
        assert filter_viewable(admin_user, repos) == repos

    def test_no_rows_returns_empty(self, linked_member, db, _no_backstop_enqueue):
        assert viewable_repo_ids(linked_member, ["a/b"]) == set()

    def test_no_social_account_returns_empty(self, member_user, fresh_sync):
        # No linked platform identity (uid=None) -> empty viewable set, regardless of any rows.
        _grant("101", "a/b", RepoAccessLevel.READ)
        assert viewable_repo_ids(member_user, ["a/b"]) == set()


class TestSearchViewableRepositories:
    def test_member_sees_only_accessible_repos(self, linked_member, fresh_sync):
        _catalog("a/one")
        _catalog("a/two")
        _grant("101", "a/one", RepoAccessLevel.READ)

        result = search_viewable_repositories(linked_member, limit=10)

        assert [r.slug for r in result] == ["a/one"]

    def test_admin_sees_all_fresh_catalog(self, admin_user, fresh_sync):
        _catalog("a/one")
        _catalog("b/two")

        result = search_viewable_repositories(admin_user, limit=10)

        assert {r.slug for r in result} == {"a/one", "b/two"}

    def test_excludes_stale_catalog_rows(self, admin_user, fresh_sync):
        _catalog("a/one")
        _catalog("b/stale", synced_at=timezone.now() - timedelta(hours=48))

        result = search_viewable_repositories(admin_user, limit=10)

        assert {r.slug for r in result} == {"a/one"}

    def test_search_matches_slug_or_name(self, admin_user, fresh_sync):
        _catalog("team/alpha", name="Alpha service")
        _catalog("team/beta", name="Beta service")

        result = search_viewable_repositories(admin_user, search="alph", limit=10)

        assert [r.slug for r in result] == ["team/alpha"]

    def test_topics_are_and_matched(self, admin_user, fresh_sync):
        _catalog("a/one", topics=["python", "api"])
        _catalog("a/two", topics=["python"])

        result = search_viewable_repositories(admin_user, topics=["python", "api"], limit=10)

        assert [r.slug for r in result] == ["a/one"]

    def test_unlinked_member_sees_nothing(self, member_user, fresh_sync):
        _catalog("a/one")
        _grant("999", "a/one", RepoAccessLevel.READ)

        assert search_viewable_repositories(member_user, limit=10) == []

    def test_limit_caps_results(self, admin_user, fresh_sync):
        for i in range(5):
            _catalog(f"a/r{i}")

        assert len(search_viewable_repositories(admin_user, limit=3)) == 3
