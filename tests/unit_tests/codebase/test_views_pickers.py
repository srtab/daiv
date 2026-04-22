"""Tests for the HTMX picker views in codebase.views."""

from __future__ import annotations

from unittest.mock import Mock, patch

from django.test import Client
from django.urls import reverse

import pytest

from accounts.models import User
from codebase.base import GitPlatform, Repository


def _repo(slug: str, name: str | None = None, default_branch: str = "main") -> Repository:
    return Repository(
        pk=hash(slug) % (2**31),
        slug=slug,
        name=name or slug.split("/")[-1],
        clone_url=f"https://example/{slug}.git",
        html_url=f"https://example/{slug}",
        default_branch=default_branch,
        git_platform=GitPlatform.GITLAB,
        topics=[],
    )


@pytest.fixture
def logged_in_client(db) -> Client:
    user = User.objects.create_user(username="picker", email="picker@test.com", password="x")  # noqa: S106
    client = Client()
    client.force_login(user)
    return client


class TestRepoPickerView:
    def test_requires_login(self, db):
        """Anonymous GET is redirected to the login page."""
        client = Client()
        url = reverse("codebase:picker-repositories")
        resp = client.get(url)
        assert resp.status_code == 302

    @patch("codebase.views.RepoClient")
    def test_lists_repositories_without_query(self, mock_repo_client, logged_in_client):
        """Empty `q` still renders the list (no min-length gate)."""
        instance = Mock()
        instance.list_repositories.return_value = [_repo("acme/api"), _repo("acme/web")]
        mock_repo_client.create_instance.return_value = instance

        resp = logged_in_client.get(reverse("codebase:picker-repositories"))

        assert resp.status_code == 200
        instance.list_repositories.assert_called_once_with(search=None, limit=20)
        assert b"acme/api" in resp.content
        assert b"acme/web" in resp.content

    @patch("codebase.views.RepoClient")
    def test_passes_q_to_list_repositories(self, mock_repo_client, logged_in_client):
        """`?q=foo` is forwarded as `search="foo"`."""
        instance = Mock()
        instance.list_repositories.return_value = []
        mock_repo_client.create_instance.return_value = instance

        logged_in_client.get(reverse("codebase:picker-repositories") + "?q=foo")

        instance.list_repositories.assert_called_once_with(search="foo", limit=20)

    @patch("codebase.views.RepoClient")
    def test_renders_empty_state_on_client_exception(self, mock_repo_client, logged_in_client):
        """Client errors render the empty-state template with an error row."""
        instance = Mock()
        instance.list_repositories.side_effect = RuntimeError("boom")
        mock_repo_client.create_instance.return_value = instance

        resp = logged_in_client.get(reverse("codebase:picker-repositories"))

        assert resp.status_code == 200
        assert b"Could not load repositories" in resp.content


class TestBranchPickerView:
    def test_requires_login(self, db):
        client = Client()
        url = reverse("codebase:picker-branches", kwargs={"slug": "acme/api"})
        resp = client.get(url)
        assert resp.status_code == 302

    @patch("codebase.views.RepoClient")
    def test_lists_branches(self, mock_repo_client, logged_in_client):
        instance = Mock()
        instance.list_branches.return_value = ["main", "feat/one"]
        mock_repo_client.create_instance.return_value = instance

        resp = logged_in_client.get(reverse("codebase:picker-branches", kwargs={"slug": "acme/api"}))

        assert resp.status_code == 200
        instance.list_branches.assert_called_once_with("acme/api", search=None, limit=20)
        assert b"main" in resp.content
        assert b"feat/one" in resp.content

    @patch("codebase.views.RepoClient")
    def test_passes_q_to_list_branches(self, mock_repo_client, logged_in_client):
        instance = Mock()
        instance.list_branches.return_value = []
        mock_repo_client.create_instance.return_value = instance

        logged_in_client.get(reverse("codebase:picker-branches", kwargs={"slug": "acme/api"}) + "?q=feat")

        instance.list_branches.assert_called_once_with("acme/api", search="feat", limit=20)

    @patch("codebase.views.RepoClient")
    def test_marks_selected_branch(self, mock_repo_client, logged_in_client):
        instance = Mock()
        instance.list_branches.return_value = ["main", "feat/one"]
        mock_repo_client.create_instance.return_value = instance

        resp = logged_in_client.get(reverse("codebase:picker-branches", kwargs={"slug": "acme/api"}) + "?selected=main")

        # Selected row gets the ✓ marker (emerald check).
        assert b"text-emerald-400" in resp.content

    @patch("codebase.views.RepoClient")
    def test_renders_error_row_on_client_exception(self, mock_repo_client, logged_in_client):
        instance = Mock()
        instance.list_branches.side_effect = RuntimeError("boom")
        mock_repo_client.create_instance.return_value = instance

        resp = logged_in_client.get(reverse("codebase:picker-branches", kwargs={"slug": "acme/api"}))

        assert resp.status_code == 200
        assert b"Could not load branches" in resp.content

    @patch("codebase.views.RepoClient")
    def test_slug_supports_slashes(self, mock_repo_client, logged_in_client):
        """`<path:slug>` accepts multi-segment namespaces (e.g. GitLab groups)."""
        instance = Mock()
        instance.list_branches.return_value = ["main"]
        mock_repo_client.create_instance.return_value = instance

        resp = logged_in_client.get(reverse("codebase:picker-branches", kwargs={"slug": "group/subgroup/repo"}))

        assert resp.status_code == 200
        instance.list_branches.assert_called_once_with("group/subgroup/repo", search=None, limit=20)
