"""Per-user filtering on the repository search autocomplete."""

from __future__ import annotations

from unittest.mock import Mock, patch

from django.test import Client

import pytest

from codebase.base import GitPlatform, Repository


def _repo(slug: str) -> Repository:
    return Repository(
        pk=abs(hash(slug)) % (2**31),
        slug=slug,
        name=slug.split("/")[-1],
        clone_url=f"https://x/{slug}.git",
        html_url=f"https://x/{slug}",
        default_branch="main",
        git_platform=GitPlatform.GITLAB,
        topics=[],
    )


@pytest.mark.django_db
def test_search_repositories_filters_to_viewable(member_user, mock_repo_client):
    mock_repo_client.list_repositories.return_value = [_repo("acme/api"), _repo("acme/secret")]

    client = Client()
    client.force_login(member_user)

    def _only_api(user, repos):
        return [r for r in repos if r.slug == "acme/api"]

    with patch("codebase.api.router.filter_viewable", new=Mock(side_effect=_only_api)):
        resp = client.get("/api/codebase/repositories/search?q=acme")

    assert resp.status_code == 200
    assert [row["slug"] for row in resp.json()] == ["acme/api"]
