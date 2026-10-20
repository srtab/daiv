"""Every GitLab project and GitHub App already calls these URLs, so they must not move with the router."""

from django.urls import resolve

import pytest


@pytest.mark.parametrize(
    "path",
    [
        "/api/codebase/callbacks/gitlab",
        "/api/codebase/callbacks/gitlab/",
        "/api/codebase/callbacks/github",
        "/api/codebase/callbacks/github/",
    ],
)
def test_webhook_urls_are_unchanged(path):
    assert resolve(path).route == path.removeprefix("/")
