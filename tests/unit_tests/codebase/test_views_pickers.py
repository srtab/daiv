"""Tests for the HTMX picker views in codebase.views."""

from __future__ import annotations

import json
from unittest.mock import ANY, Mock, patch

from django.test import Client
from django.urls import reverse

import pytest
from bs4 import BeautifulSoup
from gitlab.exceptions import GitlabError
from sessions.validators import MAX_REPOS_PER_BATCH

from accounts.models import User
from codebase.authorization import RepositoryFacets, RepositoryMatches
from codebase.views import SELECT_ALL_CANDIDATES


def _cat(slug: str, name: str | None = None, default_branch: str = "main", languages: dict | None = None):
    from codebase.models import RepositoryCatalog

    return RepositoryCatalog(
        provider="gitlab",
        slug=slug,
        name=name if name is not None else slug.split("/")[-1],
        default_branch=default_branch,
        html_url=f"https://example/{slug}",
        topics=[],
        languages=languages or {},
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

    @patch("codebase.views.search_runnable_repositories", autospec=True)
    def test_lists_repositories_without_query(self, mock_search, logged_in_client):
        """Empty `q` still renders the list (no min-length gate) and passes search=None."""
        mock_search.return_value = [_cat("acme/api"), _cat("acme/web")]

        resp = logged_in_client.get(reverse("codebase:picker-repositories"))

        assert resp.status_code == 200
        mock_search.assert_called_once_with(ANY, search=None, topics=None, language=None, limit=11, after_slug=None)
        assert b"acme/api" in resp.content
        assert b"acme/web" in resp.content

    @patch("codebase.views.search_runnable_repositories", autospec=True)
    def test_passes_q_as_search(self, mock_search, logged_in_client):
        """`?q=foo` is forwarded as `search="foo"`."""
        mock_search.return_value = []

        logged_in_client.get(reverse("codebase:picker-repositories") + "?q=foo")

        mock_search.assert_called_once_with(ANY, search="foo", topics=None, language=None, limit=11, after_slug=None)

    @patch("codebase.views.search_runnable_repositories", autospec=True)
    def test_renders_rows_in_returned_order(self, mock_search, logged_in_client):
        """The view renders rows in the order the query returned them (slug-ordered)."""
        mock_search.return_value = [_cat("acme/api"), _cat("acme/beta"), _cat("acme/zeta")]

        resp = logged_in_client.get(reverse("codebase:picker-repositories"))

        body = resp.content.decode()
        assert body.index("acme/api") < body.index("acme/beta") < body.index("acme/zeta")

    @patch("codebase.views.search_runnable_repositories", autospec=True)
    def test_renders_empty_state(self, mock_search, logged_in_client):
        mock_search.return_value = []

        resp = logged_in_client.get(reverse("codebase:picker-repositories"))

        assert resp.status_code == 200
        assert b"No repositories found" in resp.content

    @pytest.mark.parametrize("count", [1, 10], ids=["partial-page", "exactly-one-full-page"])
    @patch("codebase.views.search_runnable_repositories", autospec=True)
    def test_no_sentinel_unless_a_probe_row_came_back(self, mock_search, logged_in_client, count):
        """Anything up to PICKER_LIMIT rows means the probe row was absent — no next page."""
        mock_search.return_value = [_cat(f"acme/r{i:02d}") for i in range(count)]

        resp = logged_in_client.get(reverse("codebase:picker-repositories"))

        assert resp.content.count(b"<li") == count
        assert b"intersect once" not in resp.content

    @patch("codebase.views.search_runnable_repositories", autospec=True)
    def test_overflow_renders_sentinel_anchored_on_last_shown_slug(self, mock_search, logged_in_client):
        """A full page + 1 renders 10 rows and a sentinel requesting `after=<10th slug>`."""
        mock_search.return_value = [_cat(f"acme/r{i:02d}") for i in range(11)]

        resp = logged_in_client.get(reverse("codebase:picker-repositories") + "?q=acme")

        body = resp.content.decode()
        assert body.count("<li") == 11  # 10 rows + sentinel
        assert "acme/r10" not in body  # the probe row is not rendered
        assert 'hx-trigger="intersect once root:#repo-picker-list"' in body
        assert "after=acme%2Fr09" in body
        assert "q=acme" in body

    @patch("codebase.views.search_runnable_repositories", autospec=True)
    def test_after_forwards_keyset_and_renders_rows_without_wrapper(self, mock_search, logged_in_client):
        """The next-page fragment must be bare <li> rows: it replaces a sentinel inside the <ul>."""
        mock_search.return_value = [_cat("acme/web")]

        resp = logged_in_client.get(reverse("codebase:picker-repositories") + "?q=acme&after=acme/r09")

        mock_search.assert_called_once_with(
            ANY, search="acme", topics=None, language=None, limit=11, after_slug="acme/r09"
        )
        body = resp.content.decode()
        assert "<ul>" not in body
        assert "acme/web" in body

    @patch("codebase.views.search_runnable_repositories", autospec=True)
    def test_overflowing_next_page_emits_its_own_sentinel(self, mock_search, logged_in_client):
        """Page 2 must re-emit the sentinel, or infinite scroll silently stops after 20 repos."""
        mock_search.return_value = [_cat(f"acme/r{i:02d}") for i in range(10, 21)]

        resp = logged_in_client.get(reverse("codebase:picker-repositories") + "?after=acme/r09")

        body = resp.content.decode()
        assert 'hx-trigger="intersect once root:#repo-picker-list"' in body
        assert "after=acme%2Fr19" in body

    @patch("codebase.views.search_runnable_repositories", autospec=True)
    def test_exhausted_next_page_renders_nothing(self, mock_search, logged_in_client):
        """No rows left after the cursor → empty fragment, not the first page's empty state
        (which would replace the sentinel with "No repositories found" under a full list)."""
        mock_search.return_value = []

        resp = logged_in_client.get(reverse("codebase:picker-repositories") + "?after=acme/zzz")

        assert resp.status_code == 200
        assert resp.content.strip() == b""


class TestRepoPickerFilters:
    @patch("codebase.views.runnable_repository_matches", autospec=True)
    @patch("codebase.views.search_runnable_repositories", autospec=True)
    def test_forwards_language_and_topic(self, mock_search, mock_matches, logged_in_client):
        mock_search.return_value = []
        mock_matches.return_value = RepositoryMatches(0, [], [])

        logged_in_client.get(reverse("codebase:picker-repositories") + "?language=Python&topic=api")

        mock_search.assert_called_once_with(
            ANY, search=None, topics=["api"], language="Python", limit=11, after_slug=None
        )

    @patch("codebase.views.runnable_repository_matches", autospec=True)
    @patch("codebase.views.search_runnable_repositories", autospec=True)
    def test_next_page_url_carries_filters(self, mock_search, mock_matches, logged_in_client):
        mock_search.return_value = [_cat(f"acme/r{i:02d}") for i in range(11)]
        mock_matches.return_value = RepositoryMatches(11, [], [])

        resp = logged_in_client.get(reverse("codebase:picker-repositories") + "?language=Python&topic=api")

        body = resp.content.decode()
        assert "language=Python" in body
        assert "topic=api" in body

    @patch("codebase.views.runnable_repository_matches", autospec=True)
    @patch("codebase.views.search_runnable_repositories", autospec=True)
    def test_select_all_data_with_a_filter_on_the_first_page(self, mock_search, mock_matches, logged_in_client):
        mock_search.return_value = [_cat("acme/a")]
        mock_matches.return_value = RepositoryMatches(2, [_cat("acme/a")], ["acme/b"])

        resp = logged_in_client.get(reverse("codebase:picker-repositories") + "?language=Python")

        node = BeautifulSoup(resp.content, "html.parser").select_one("[data-select-all]")
        assert json.loads(node["data-select-all"]) == {
            "total": 2,
            "candidates": [{"slug": "acme/a", "default_branch": "main"}],
            "matching_selected": ["acme/b"],
        }

    @pytest.mark.parametrize("query", ["", "?language=Python&after=acme/r09"], ids=["no-filter", "next-page"])
    @patch("codebase.views.runnable_repository_matches", autospec=True)
    @patch("codebase.views.search_runnable_repositories", autospec=True)
    def test_no_select_all_data_without_filter_or_past_page_one(
        self, mock_search, mock_matches, logged_in_client, query
    ):
        mock_search.return_value = [_cat("acme/a")]

        resp = logged_in_client.get(reverse("codebase:picker-repositories") + query)

        assert b"data-select-all" not in resp.content
        mock_matches.assert_not_called()

    @patch("codebase.views.runnable_repository_matches", autospec=True)
    @patch("codebase.views.search_runnable_repositories", autospec=True)
    def test_selected_param_is_trimmed_and_capped(self, mock_search, mock_matches, logged_in_client):
        mock_search.return_value = []
        mock_matches.return_value = RepositoryMatches(0, [], [])
        url = reverse("codebase:picker-repositories")

        logged_in_client.get(url, {"q": "a", "selected": " a/1 ,,a/2,"})
        logged_in_client.get(url, {"q": "a", "selected": ",".join(f"a/{i}" for i in range(100))})

        first, second = mock_matches.call_args_list
        assert first.kwargs["selected"] == ["a/1", "a/2"]
        assert len(second.kwargs["selected"]) == SELECT_ALL_CANDIDATES

    def test_select_all_candidates_cover_the_largest_field(self):
        assert SELECT_ALL_CANDIDATES >= MAX_REPOS_PER_BATCH

    @patch("codebase.views.runnable_repository_matches", autospec=True)
    @patch("codebase.views.search_runnable_repositories", autospec=True)
    def test_row_shows_filtered_language_share(self, mock_search, mock_matches, logged_in_client):
        mock_search.return_value = [_cat("acme/dash", languages={"TypeScript": 52.0, "Python": 41.0})]
        mock_matches.return_value = RepositoryMatches(1, [], [])
        url = reverse("codebase:picker-repositories")

        filtered = logged_in_client.get(url + "?language=Python").content.decode()
        unfiltered = logged_in_client.get(url).content.decode()

        assert "Python 41%" in filtered
        assert "TypeScript" in unfiltered
        assert "Python 41%" not in unfiltered


class TestRepoFacetsView:
    def test_requires_login(self, db):
        resp = Client().get(reverse("codebase:picker-repository-facets"))
        assert resp.status_code == 302

    @patch("codebase.views.runnable_repository_facets", autospec=True)
    def test_renders_counts_and_hides_empty_dimension(self, mock_facets, logged_in_client):
        mock_facets.return_value = RepositoryFacets(languages=[("Python", 3)], topics=[])

        resp = logged_in_client.get(reverse("codebase:picker-repository-facets"))

        body = resp.content.decode()
        assert "pickFilter('language', 'Python')" in body
        assert ">3</span>" in body
        assert "toggleFilterMenu('topic')" not in body
        mock_facets.assert_called_once_with(ANY, topic=None, language=None)

    @patch("codebase.views.runnable_repository_facets", autospec=True)
    def test_keeps_a_set_dimension_without_options(self, mock_facets, logged_in_client):
        mock_facets.return_value = RepositoryFacets(languages=[], topics=[])

        resp = logged_in_client.get(reverse("codebase:picker-repository-facets") + "?topic=api")

        assert "toggleFilterMenu('topic')" in resp.content.decode()
        mock_facets.assert_called_once_with(ANY, topic="api", language=None)

    @patch("codebase.views.runnable_repository_facets", autospec=True)
    def test_both_menus_offer_a_find_box_that_never_submits_the_form(self, mock_facets, logged_in_client):
        mock_facets.return_value = RepositoryFacets(languages=[("Python", 3)], topics=[("api", 2)])

        resp = logged_in_client.get(reverse("codebase:picker-repository-facets"))

        finds = BeautifulSoup(resp.content, "html.parser").select(".filter-menu__find")
        assert len(finds) == 2
        assert all(find["x-model"] == "filterFind" and "@keydown.enter.prevent" in find.attrs for find in finds)
        assert "matchesFilterFind('Python')" in resp.content.decode()

    @patch("codebase.views.runnable_repository_facets", autospec=True)
    def test_facets_escape_quotes_in_option_values(self, mock_facets, logged_in_client):
        mock_facets.return_value = RepositoryFacets(languages=[("Ren'Py", 1)], topics=[])

        resp = logged_in_client.get(reverse("codebase:picker-repository-facets"))

        assert "pickFilter('language', 'Ren\\u0027Py')" in resp.content.decode()


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
