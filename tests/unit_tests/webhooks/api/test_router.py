"""Every GitLab project and GitHub App already calls these URLs, and ``setup_webhooks`` reverses
``api:gitlab_callback``, so the paths and names must not move with the router."""

from django.urls import resolve, reverse

import pytest


@pytest.mark.parametrize(
    ("path", "url_name"),
    [
        ("/api/codebase/callbacks/gitlab", "callback"),
        ("/api/codebase/callbacks/gitlab/", "gitlab_callback"),
        ("/api/codebase/callbacks/github", "callback"),
        ("/api/codebase/callbacks/github/", "callback"),
    ],
)
def test_webhook_urls_are_unchanged(path, url_name):
    match = resolve(path)
    assert (match.route, match.view_name) == (path.removeprefix("/"), f"api:{url_name}")


def test_gitlab_callback_reverses_to_the_slashed_path():
    assert reverse("api:gitlab_callback") == "/api/codebase/callbacks/gitlab/"
