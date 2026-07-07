"""Per-user filtering on the repository search autocomplete (served from the local catalog)."""

from __future__ import annotations

from unittest.mock import ANY, patch

from django.test import Client

import pytest

from codebase.models import RepositoryCatalog


def _cat(slug: str) -> RepositoryCatalog:
    return RepositoryCatalog(
        provider="gitlab",
        slug=slug,
        name=slug.split("/")[-1],
        default_branch="main",
        html_url=f"https://x/{slug}",
        topics=[],
    )


@pytest.mark.django_db
def test_search_repositories_returns_viewable_rows(member_user):
    client = Client()
    client.force_login(member_user)

    with patch("codebase.api.router.search_viewable_repositories", return_value=[_cat("acme/api")]) as mock_search:
        resp = client.get("/api/codebase/repositories/search?q=acme")

    assert resp.status_code == 200
    assert [row["slug"] for row in resp.json()] == ["acme/api"]
    mock_search.assert_called_once_with(ANY, search="acme", limit=10)


@pytest.mark.django_db
def test_search_repositories_short_query_short_circuits(member_user):
    client = Client()
    client.force_login(member_user)

    with patch("codebase.api.router.search_viewable_repositories") as mock_search:
        resp = client.get("/api/codebase/repositories/search?q=a")

    assert resp.json() == []
    mock_search.assert_not_called()
