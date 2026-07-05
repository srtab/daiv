from unittest.mock import patch

from django.utils import timezone

import pytest

from codebase.base import GitPlatform, RepoAccessLevel, RepoMember, Repository
from codebase.conf import settings as codebase_settings
from codebase.models import RepositoryAccess, RepositoryAccessSyncState
from codebase.tasks import sync_repository_access_cron_task


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


def _row(repo_id: str, uid: str, level=RepoAccessLevel.READ) -> RepositoryAccess:
    return RepositoryAccess.objects.create(
        provider="gitlab", uid=uid, username=f"user{uid}", repo_id=repo_id, access_level=level, synced_at=timezone.now()
    )


@pytest.mark.django_db
class TestSyncRepositoryAccess:
    def test_mirrors_members_and_marks_success(self, mock_repo_client):
        mock_repo_client.list_repositories.return_value = [_repo("a/b")]
        mock_repo_client.list_repository_members.return_value = [
            RepoMember(uid="1", username="alice", access_level=RepoAccessLevel.WRITE),
            RepoMember(uid="2", username="bob", access_level=RepoAccessLevel.READ),
        ]

        sync_repository_access_cron_task.func()

        rows = {r.uid: r for r in RepositoryAccess.objects.filter(repo_id="a/b")}
        assert set(rows) == {"1", "2"}
        assert rows["1"].access_level == "write"
        assert rows["2"].access_level == "read"
        state = RepositoryAccessSyncState.objects.get(pk=RepositoryAccessSyncState.SINGLETON_PK)
        assert state.status == RepositoryAccessSyncState.Status.OK
        assert state.last_success_at is not None

    def test_replaces_previous_rows_and_prunes_vanished_repos(self, mock_repo_client):
        _row("a/b", "99")
        _row("old/gone", "1")
        mock_repo_client.list_repositories.return_value = [_repo("a/b")]
        mock_repo_client.list_repository_members.return_value = [
            RepoMember(uid="1", username="alice", access_level=RepoAccessLevel.READ)
        ]

        sync_repository_access_cron_task.func()

        assert not RepositoryAccess.objects.filter(uid="99").exists()
        assert not RepositoryAccess.objects.filter(repo_id="old/gone").exists()
        assert RepositoryAccess.objects.filter(repo_id="a/b", uid="1").exists()

    def test_per_repo_failure_keeps_previous_rows_and_marks_failed(self, mock_repo_client):
        _row("a/b", "7", RepoAccessLevel.WRITE)
        mock_repo_client.list_repositories.return_value = [_repo("a/b"), _repo("c/d")]
        mock_repo_client.list_repository_members.side_effect = [
            Exception("boom"),
            [RepoMember(uid="8", username="carol", access_level=RepoAccessLevel.READ)],
        ]

        sync_repository_access_cron_task.func()

        assert RepositoryAccess.objects.filter(repo_id="a/b", uid="7").exists()  # stale rows kept
        assert RepositoryAccess.objects.filter(repo_id="c/d", uid="8").exists()  # sibling synced
        state = RepositoryAccessSyncState.objects.get(pk=RepositoryAccessSyncState.SINGLETON_PK)
        assert state.status == RepositoryAccessSyncState.Status.FAILED
        assert state.last_success_at is None

    def test_listing_failure_marks_failed_and_keeps_rows(self, mock_repo_client):
        _row("a/b", "7")
        mock_repo_client.list_repositories.side_effect = Exception("api down")

        sync_repository_access_cron_task.func()

        assert RepositoryAccess.objects.filter(repo_id="a/b").exists()
        state = RepositoryAccessSyncState.objects.get(pk=RepositoryAccessSyncState.SINGLETON_PK)
        assert state.status == RepositoryAccessSyncState.Status.FAILED

    def test_write_failure_keeps_previous_rows_and_marks_failed(self, mock_repo_client):
        """A DB write failure (e.g. duplicate uid in the fetched member list) must not escape the
        per-repo try/except: it should be isolated like a listing failure, leaving prior rows
        untouched (serve-stale) and letting sibling repos still sync.
        """
        _row("a/b", "7", RepoAccessLevel.WRITE)
        mock_repo_client.list_repositories.return_value = [_repo("a/b"), _repo("c/d")]
        mock_repo_client.list_repository_members.side_effect = [
            [RepoMember(uid="9", username="dave", access_level=RepoAccessLevel.READ)],
            [RepoMember(uid="8", username="carol", access_level=RepoAccessLevel.READ)],
        ]

        real_bulk_create = RepositoryAccess.objects.bulk_create
        calls = []

        def _bulk_create(rows, *args, **kwargs):
            calls.append(rows)
            if len(calls) == 1:
                raise Exception("boom")
            return real_bulk_create(rows, *args, **kwargs)

        with patch.object(RepositoryAccess.objects, "bulk_create", side_effect=_bulk_create):
            sync_repository_access_cron_task.func()

        assert RepositoryAccess.objects.filter(repo_id="a/b", uid="7").exists()  # stale rows kept
        assert not RepositoryAccess.objects.filter(repo_id="a/b", uid="9").exists()  # failed write not applied
        assert RepositoryAccess.objects.filter(repo_id="c/d", uid="8").exists()  # sibling synced
        state = RepositoryAccessSyncState.objects.get(pk=RepositoryAccessSyncState.SINGLETON_PK)
        assert state.status == RepositoryAccessSyncState.Status.FAILED
        assert state.last_success_at is None

    def test_swe_platform_is_a_noop(self, mock_repo_client):
        with patch.object(codebase_settings, "CLIENT", GitPlatform.SWE):
            sync_repository_access_cron_task.func()

        assert not mock_repo_client.list_repositories.called
        assert not RepositoryAccess.objects.exists()
        assert not RepositoryAccessSyncState.objects.exists()
