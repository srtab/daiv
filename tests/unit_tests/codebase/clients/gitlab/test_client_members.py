from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from codebase.base import RepoAccessLevel
from codebase.clients.gitlab.client import GitLabClient


def _member(member_id, username, access_level, state="active"):
    return SimpleNamespace(id=member_id, username=username, access_level=access_level, state=state)


@pytest.fixture
def gitlab_client():
    with patch("codebase.clients.gitlab.client.Gitlab", return_value=Mock()):
        yield GitLabClient(auth_token="test-token", url="https://gitlab.com")  # noqa: S106


class TestListRepositoryMembers:
    def _install_members(self, gitlab_client, members):
        project = Mock()
        project.members_all.list.return_value = iter(members)
        gitlab_client.client.projects.get.return_value = project
        return project

    def test_maps_access_levels_to_tiers(self, gitlab_client):
        self._install_members(
            gitlab_client,
            [
                _member(1, "guest", 10),  # Guest — omitted
                _member(2, "planner", 15),  # Planner — omitted
                _member(3, "reporter", 20),  # Reporter — READ
                _member(4, "dev", 30),  # Developer — WRITE
                _member(5, "owner", 50),  # Owner — WRITE
            ],
        )

        result = gitlab_client.list_repository_members("group/repo")

        assert [(m.uid, m.access_level) for m in result] == [
            ("3", RepoAccessLevel.READ),
            ("4", RepoAccessLevel.WRITE),
            ("5", RepoAccessLevel.WRITE),
        ]
        assert result[0].username == "reporter"

    def test_deduplicates_by_uid_keeping_highest_level(self, gitlab_client):
        # members/all can surface a user more than once (direct + inherited + shared-group).
        # The result must have one entry per uid at the highest level to satisfy the
        # (provider, uid, repo_id) uniqueness the sync task relies on.
        self._install_members(
            gitlab_client,
            [
                _member(3, "reporter", 20),  # READ
                _member(3, "reporter", 30),  # same user, WRITE via another grant
            ],
        )

        result = gitlab_client.list_repository_members("group/repo")

        assert [(m.uid, m.access_level) for m in result] == [("3", RepoAccessLevel.WRITE)]

    def test_dedup_keeps_write_regardless_of_order(self, gitlab_client):
        self._install_members(
            gitlab_client,
            [
                _member(3, "reporter", 30),  # WRITE first
                _member(3, "reporter", 20),  # then a READ grant — must not downgrade
            ],
        )

        result = gitlab_client.list_repository_members("group/repo")

        assert [(m.uid, m.access_level) for m in result] == [("3", RepoAccessLevel.WRITE)]

    def test_skips_non_active_members(self, gitlab_client):
        self._install_members(
            gitlab_client, [_member(1, "blocked", 40, state="blocked"), _member(2, "waiting", 40, state="awaiting")]
        )

        assert gitlab_client.list_repository_members("group/repo") == []

    def test_uses_members_all_with_lazy_project(self, gitlab_client):
        project = self._install_members(gitlab_client, [])

        gitlab_client.list_repository_members("group/repo")

        gitlab_client.client.projects.get.assert_called_once_with("group/repo", lazy=True)
        project.members_all.list.assert_called_once_with(iterator=True)
