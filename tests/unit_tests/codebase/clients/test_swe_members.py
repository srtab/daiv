import pytest

from codebase.base import RepoAccessLevel, RepoMember
from codebase.clients import RepoClient
from codebase.clients.swe import SWERepoClient


def test_repo_client_declares_member_listing():
    assert "list_repository_members" in RepoClient.__abstractmethods__


def test_repo_member_model():
    member = RepoMember(uid="42", username="alice", access_level=RepoAccessLevel.WRITE)
    assert member.uid == "42"
    assert member.access_level == "write"


def test_swe_client_does_not_support_member_listing():
    client = SWERepoClient()
    with pytest.raises(NotImplementedError):
        client.list_repository_members("owner/repo")
