from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codebase.base import RepoAccessLevel
from codebase.clients.github.client import GitHubClient


def _collaborator(collaborator_id, login, *, admin=False, maintain=False, push=False, triage=False, pull=False):
    permissions = SimpleNamespace(admin=admin, maintain=maintain, push=push, triage=triage, pull=pull)
    return SimpleNamespace(id=collaborator_id, login=login, permissions=permissions)


@pytest.fixture
def github_client():
    integration = Mock()
    mock_installation = Mock()
    mock_github = Mock()
    mock_installation.get_github_for_installation.return_value = mock_github
    integration.get_app_installation.return_value = mock_installation
    return GitHubClient(integration=integration, installation_id=67890)


class TestListRepositoryMembers:
    def _install_collaborators(self, github_client, collaborators):
        repo = Mock()
        repo.get_collaborators.return_value = iter(collaborators)
        github_client.client.get_repo.return_value = repo
        return repo

    def test_maps_permissions_to_tiers(self, github_client):
        self._install_collaborators(
            github_client,
            [
                _collaborator(1, "reader", pull=True),
                _collaborator(2, "triager", pull=True, triage=True),
                _collaborator(3, "writer", pull=True, triage=True, push=True),
                _collaborator(4, "maintainer", pull=True, push=True, maintain=True),
                _collaborator(5, "admin", pull=True, push=True, admin=True),
                _collaborator(6, "nobody"),
            ],
        )

        result = github_client.list_repository_members("owner/repo")

        assert [(m.uid, m.access_level) for m in result] == [
            ("1", RepoAccessLevel.READ),
            ("2", RepoAccessLevel.READ),
            ("3", RepoAccessLevel.WRITE),
            ("4", RepoAccessLevel.WRITE),
            ("5", RepoAccessLevel.WRITE),
        ]
        assert result[0].username == "reader"

    def test_uses_lazy_repo(self, github_client):
        repo = self._install_collaborators(github_client, [])

        github_client.list_repository_members("owner/repo")

        github_client.client.get_repo.assert_called_once_with("owner/repo", lazy=True)
        repo.get_collaborators.assert_called_once_with()
