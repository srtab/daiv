from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

import pytest

from codebase.base import GitPlatform, RepoAccessLevel, RepoMember, Repository
from codebase.conf import settings as codebase_settings
from codebase.models import RepositoryAccess, RepositoryAccessSyncState, RepositoryCatalog
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

    def test_empty_members_with_prior_rows_keeps_them_and_marks_failed(self, mock_repo_client):
        # An empty member list for a repo that had members is treated as a degraded response:
        # keep the previous rows (serve-stale) rather than silently wiping access, and count it
        # as a failure so last_success_at does not advance.
        _row("a/b", "7", RepoAccessLevel.WRITE)
        mock_repo_client.list_repositories.return_value = [_repo("a/b")]
        mock_repo_client.list_repository_members.return_value = []

        sync_repository_access_cron_task.func()

        assert RepositoryAccess.objects.filter(repo_id="a/b", uid="7").exists()
        state = RepositoryAccessSyncState.objects.get(pk=RepositoryAccessSyncState.SINGLETON_PK)
        assert state.status == RepositoryAccessSyncState.Status.FAILED
        assert state.last_success_at is None

    def test_empty_members_without_prior_rows_is_clean(self, mock_repo_client):
        # A repo that legitimately has no members and no prior rows is not a failure.
        mock_repo_client.list_repositories.return_value = [_repo("a/b")]
        mock_repo_client.list_repository_members.return_value = []

        sync_repository_access_cron_task.func()

        assert not RepositoryAccess.objects.filter(repo_id="a/b").exists()
        state = RepositoryAccessSyncState.objects.get(pk=RepositoryAccessSyncState.SINGLETON_PK)
        assert state.status == RepositoryAccessSyncState.Status.OK
        assert state.last_success_at is not None

    def test_empty_universe_with_rows_skips_prune_and_marks_failed(self, mock_repo_client):
        # An empty repository listing while rows exist (e.g. a transient scope change) must not
        # wipe every access row; the prune is skipped, rows are preserved, and the run is marked
        # failed rather than reading as a clean success.
        _row("a/b", "7", RepoAccessLevel.WRITE)
        mock_repo_client.list_repositories.return_value = []

        sync_repository_access_cron_task.func()

        assert RepositoryAccess.objects.filter(repo_id="a/b", uid="7").exists()
        assert not mock_repo_client.list_repository_members.called
        state = RepositoryAccessSyncState.objects.get(pk=RepositoryAccessSyncState.SINGLETON_PK)
        assert state.status == RepositoryAccessSyncState.Status.FAILED
        assert state.last_success_at is None

    def test_empty_universe_without_rows_is_clean(self, mock_repo_client):
        # A genuinely empty install (no repos, no rows) is a clean success, not a failure.
        mock_repo_client.list_repositories.return_value = []

        sync_repository_access_cron_task.func()

        state = RepositoryAccessSyncState.objects.get(pk=RepositoryAccessSyncState.SINGLETON_PK)
        assert state.status == RepositoryAccessSyncState.Status.OK
        assert state.last_success_at is not None

    def test_rows_aged_past_hard_ttl_are_pruned(self, mock_repo_client):
        # Rows older than the hard TTL grant no access; the sync clears them so a genuinely
        # member-less repo self-heals and stops tripping the empty-member guard.
        stale = _row("a/b", "7", RepoAccessLevel.WRITE)
        stale.synced_at = timezone.now() - timedelta(hours=codebase_settings.REPO_ACCESS_HARD_TTL_HOURS + 1)
        stale.save(update_fields=["synced_at"])
        mock_repo_client.list_repositories.return_value = [_repo("c/d")]
        mock_repo_client.list_repository_members.return_value = [
            RepoMember(uid="8", username="carol", access_level=RepoAccessLevel.READ)
        ]

        sync_repository_access_cron_task.func()

        assert not RepositoryAccess.objects.filter(repo_id="a/b").exists()  # aged out and pruned
        assert RepositoryAccess.objects.filter(repo_id="c/d", uid="8").exists()

    def test_swe_platform_is_a_noop(self, mock_repo_client):
        with patch.object(codebase_settings, "CLIENT", GitPlatform.SWE):
            sync_repository_access_cron_task.func()

        assert not mock_repo_client.list_repositories.called
        assert not RepositoryAccess.objects.exists()
        assert not RepositoryAccessSyncState.objects.exists()


@pytest.mark.django_db
class TestSyncRepositoryCatalog:
    def test_upserts_catalog_from_universe(self, mock_repo_client):
        mock_repo_client.list_repositories.return_value = [_repo("a/b")]
        mock_repo_client.list_repository_members.return_value = [
            RepoMember(uid="1", username="alice", access_level=RepoAccessLevel.READ)
        ]

        sync_repository_access_cron_task.func()

        cat = RepositoryCatalog.objects.get(provider="gitlab", slug="a/b")
        assert cat.name == "b"
        assert cat.default_branch == "main"
        assert cat.html_url == "https://example/a/b"

    def test_upsert_updates_existing_row_in_place(self, mock_repo_client):
        RepositoryCatalog.objects.create(
            provider="gitlab",
            slug="a/b",
            name="old-name",
            default_branch="x",
            html_url="https://old",
            topics=[],
            synced_at=timezone.now() - timedelta(hours=1),
        )
        mock_repo_client.list_repositories.return_value = [_repo("a/b")]
        mock_repo_client.list_repository_members.return_value = [
            RepoMember(uid="1", username="alice", access_level=RepoAccessLevel.READ)
        ]

        sync_repository_access_cron_task.func()

        assert RepositoryCatalog.objects.filter(slug="a/b").count() == 1
        assert RepositoryCatalog.objects.get(slug="a/b").name == "b"

    def test_prunes_vanished_repos(self, mock_repo_client):
        RepositoryCatalog.objects.create(
            provider="gitlab",
            slug="old/gone",
            name="gone",
            default_branch="main",
            html_url="https://x",
            topics=[],
            synced_at=timezone.now(),
        )
        mock_repo_client.list_repositories.return_value = [_repo("a/b")]
        mock_repo_client.list_repository_members.return_value = [
            RepoMember(uid="1", username="alice", access_level=RepoAccessLevel.READ)
        ]

        sync_repository_access_cron_task.func()

        assert not RepositoryCatalog.objects.filter(slug="old/gone").exists()
        assert RepositoryCatalog.objects.filter(slug="a/b").exists()

    def test_empty_universe_keeps_catalog(self, mock_repo_client):
        RepositoryCatalog.objects.create(
            provider="gitlab",
            slug="a/b",
            name="b",
            default_branch="main",
            html_url="https://x",
            topics=[],
            synced_at=timezone.now(),
        )
        _row("a/b", "1")  # non-empty access rows → the degraded "empty universe" branch
        mock_repo_client.list_repositories.return_value = []

        sync_repository_access_cron_task.func()

        assert RepositoryCatalog.objects.filter(slug="a/b").exists()
