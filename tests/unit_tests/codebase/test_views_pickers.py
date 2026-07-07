"""Tests for the HTMX picker views in codebase.views."""

from __future__ import annotations

from unittest.mock import ANY, Mock, patch

from django.test import Client
from django.urls import reverse

import pytest
from gitlab.exceptions import GitlabError

from accounts.models import User


def _cat(slug: str, name: str | None = None, default_branch: str = "main"):
    from codebase.models import RepositoryCatalog

    return RepositoryCatalog(
        provider="gitlab",
        slug=slug,
        name=name if name is not None else slug.split("/")[-1],
        default_branch=default_branch,
        html_url=f"https://example/{slug}",
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
        resp = client.get(reverse("codebase:picker-repositories"))
        assert resp.status_code == 302

    @patch("codebase.views.search_viewable_repositories")
    def test_lists_repositories_without_query(self, mock_search, logged_in_client):
        """Empty `q` still renders the list (no min-length gate) and passes search=None."""
        mock_search.return_value = [_cat("acme/api"), _cat("acme/web")]

        resp = logged_in_client.get(reverse("codebase:picker-repositories"))

        assert resp.status_code == 200
        mock_search.assert_called_once_with(ANY, search=None, limit=10)
        assert b"acme/api" in resp.content
        assert b"acme/web" in resp.content

    @patch("codebase.views.search_viewable_repositories")
    def test_passes_q_as_search(self, mock_search, logged_in_client):
        """`?q=foo` is forwarded as `search="foo"`."""
        mock_search.return_value = []

        logged_in_client.get(reverse("codebase:picker-repositories") + "?q=foo")

        mock_search.assert_called_once_with(ANY, search="foo", limit=10)

    @patch("codebase.views.search_viewable_repositories")
    def test_renders_rows_in_returned_order(self, mock_search, logged_in_client):
        """The view renders rows in the order the query returned them (slug-ordered)."""
        mock_search.return_value = [_cat("acme/api"), _cat("acme/beta"), _cat("acme/zeta")]

        resp = logged_in_client.get(reverse("codebase:picker-repositories"))

        body = resp.content.decode()
        assert body.index("acme/api") < body.index("acme/beta") < body.index("acme/zeta")

    @patch("codebase.views.search_viewable_repositories")
    def test_renders_empty_state(self, mock_search, logged_in_client):
        mock_search.return_value = []

        resp = logged_in_client.get(reverse("codebase:picker-repositories"))

        assert resp.status_code == 200
        assert b"No repositories found" in resp.content


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
        instance.list_branches.assert_called_once_with("acme/api", search=None, limit=10)
        assert b"main" in resp.content
        assert b"feat/one" in resp.content

    @patch("codebase.views.RepoClient")
    def test_passes_q_to_list_branches(self, mock_repo_client, logged_in_client):
        instance = Mock()
        instance.list_branches.return_value = []
        mock_repo_client.create_instance.return_value = instance

        logged_in_client.get(reverse("codebase:picker-branches", kwargs={"slug": "acme/api"}) + "?q=feat")

        instance.list_branches.assert_called_once_with("acme/api", search="feat", limit=10)

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
        instance.list_branches.side_effect = GitlabError("boom")
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
        instance.list_branches.assert_called_once_with("group/subgroup/repo", search=None, limit=10)


class TestPickerAuthorization:
    @patch("codebase.views.RepoClient")
    def test_branch_picker_hidden_repo_404(self, mock_repo_client, logged_in_client):
        with patch("codebase.views.can_view", new=Mock(return_value=False)):
            resp = logged_in_client.get(reverse("codebase:picker-branches", args=["acme/secret"]))
        assert resp.status_code == 404
