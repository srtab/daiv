from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

import pytest
from allauth.socialaccount.models import SocialAccount

from codebase.authorization import (
    RepositoryAccessDenied,
    assert_can_run,
    can_view,
    filter_viewable,
    get_access_level,
    viewable_repo_ids,
)
from codebase.base import GitPlatform, RepoAccessLevel, Repository
from codebase.models import RepositoryAccess, RepositoryAccessSyncState


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
    return RepositoryAccessSyncState.objects.create(
        pk=RepositoryAccessSyncState.SINGLETON_PK,
        last_success_at=timezone.now(),
        status=RepositoryAccessSyncState.Status.OK,
    )


@pytest.fixture
def linked_member(member_user):
    SocialAccount.objects.create(user=member_user, provider="gitlab", uid="101")
    return member_user


def _grant(uid: str, repo_id: str, level: RepoAccessLevel):
    RepositoryAccess.objects.create(
        provider="gitlab", uid=uid, username="u", repo_id=repo_id, access_level=level, synced_at=timezone.now()
    )


@pytest.fixture(autouse=True)
def _no_backstop_enqueue():
    with patch("codebase.authorization._enqueue_sync") as m:
        yield m


class TestGetAccessLevel:
    def test_admin_bypasses_everything(self, admin_user, db):
        assert get_access_level(admin_user, "any/repo") == RepoAccessLevel.WRITE

    def test_member_without_social_account_denied(self, member_user, fresh_sync):
        assert get_access_level(member_user, "a/b") is None

    def test_member_with_read_row(self, linked_member, fresh_sync):
        _grant("101", "a/b", RepoAccessLevel.READ)
        assert get_access_level(linked_member, "a/b") == RepoAccessLevel.READ
        assert can_view(linked_member, "a/b") is True
        assert can_view(linked_member, "other/repo") is False

    def test_never_synced_denies_and_enqueues(self, linked_member, db, _no_backstop_enqueue):
        _grant("101", "a/b", RepoAccessLevel.WRITE)
        assert get_access_level(linked_member, "a/b") is None
        _no_backstop_enqueue.assert_called()

    def test_stale_beyond_hard_ttl_denies(self, linked_member, fresh_sync):
        fresh_sync.last_success_at = timezone.now() - timedelta(hours=25)
        fresh_sync.save(update_fields=["last_success_at"])
        _grant("101", "a/b", RepoAccessLevel.WRITE)
        assert get_access_level(linked_member, "a/b") is None

    def test_stale_within_hard_ttl_serves_and_enqueues_backstop(self, linked_member, fresh_sync, _no_backstop_enqueue):
        fresh_sync.last_success_at = timezone.now() - timedelta(hours=2)
        fresh_sync.save(update_fields=["last_success_at"])
        _grant("101", "a/b", RepoAccessLevel.WRITE)
        assert get_access_level(linked_member, "a/b") == RepoAccessLevel.WRITE
        _no_backstop_enqueue.assert_called()

    def test_fresh_sync_does_not_enqueue(self, linked_member, fresh_sync, _no_backstop_enqueue):
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

    def test_never_synced_denies(self, linked_member, db, _no_backstop_enqueue):
        _grant("101", "a/b", RepoAccessLevel.WRITE)
        with pytest.raises(RepositoryAccessDenied) as exc:
            assert_can_run(linked_member, ["a/b"])
        assert exc.value.repo_ids == ["a/b"]

    def test_stale_beyond_hard_ttl_denies(self, linked_member, fresh_sync):
        fresh_sync.last_success_at = timezone.now() - timedelta(hours=25)
        fresh_sync.save(update_fields=["last_success_at"])
        _grant("101", "a/b", RepoAccessLevel.WRITE)
        with pytest.raises(RepositoryAccessDenied) as exc:
            assert_can_run(linked_member, ["a/b"])
        assert exc.value.repo_ids == ["a/b"]


class TestViewableFilters:
    def test_viewable_repo_ids_subset(self, linked_member, fresh_sync):
        _grant("101", "a/b", RepoAccessLevel.READ)
        assert viewable_repo_ids(linked_member, ["a/b", "x/y"]) == {"a/b"}

    def test_filter_viewable_preserves_order(self, linked_member, fresh_sync):
        _grant("101", "c/d", RepoAccessLevel.WRITE)
        _grant("101", "a/b", RepoAccessLevel.READ)
        repos = [_repo("a/b"), _repo("x/y"), _repo("c/d")]
        assert [r.slug for r in filter_viewable(linked_member, repos)] == ["a/b", "c/d"]

    def test_admin_sees_all(self, admin_user, db):
        repos = [_repo("a/b"), _repo("x/y")]
        assert filter_viewable(admin_user, repos) == repos

    def test_never_synced_returns_empty(self, linked_member, db, _no_backstop_enqueue):
        _grant("101", "a/b", RepoAccessLevel.WRITE)
        assert viewable_repo_ids(linked_member, ["a/b"]) == set()

    def test_stale_beyond_hard_ttl_returns_empty(self, linked_member, fresh_sync):
        fresh_sync.last_success_at = timezone.now() - timedelta(hours=25)
        fresh_sync.save(update_fields=["last_success_at"])
        _grant("101", "a/b", RepoAccessLevel.WRITE)
        assert viewable_repo_ids(linked_member, ["a/b"]) == set()
