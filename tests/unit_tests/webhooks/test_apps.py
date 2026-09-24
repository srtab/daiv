"""``WebhooksConfig.ready()`` is what puts the platform callbacks on the API. The views live in ``webhooks`` but
register onto ``codebase.api.router``, so the URLs every GitLab project and GitHub App already call stay put."""

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
