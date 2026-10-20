import pytest
from pydantic import SecretStr
from webhooks.checks import check_webhook_secret
from webhooks.conf import settings as webhooks_settings

from codebase.base import GitPlatform
from codebase.conf import settings as codebase_settings


@pytest.mark.parametrize(
    "platform,secret_name,check_id",
    [
        (GitPlatform.GITLAB, "GITLAB_WEBHOOK_SECRET", "webhooks.E001"),
        (GitPlatform.GITHUB, "GITHUB_WEBHOOK_SECRET", "webhooks.E002"),
    ],
)
@pytest.mark.parametrize("missing", [None, SecretStr("")])
def test_a_missing_secret_for_the_active_platform_is_an_error(monkeypatch, platform, secret_name, check_id, missing):
    monkeypatch.setattr(codebase_settings, "CLIENT", platform)
    monkeypatch.setattr(webhooks_settings, secret_name, missing)

    assert [error.id for error in check_webhook_secret(None)] == [check_id]


@pytest.mark.parametrize(
    "platform,other_secret_name",
    [(GitPlatform.GITLAB, "GITHUB_WEBHOOK_SECRET"), (GitPlatform.GITHUB, "GITLAB_WEBHOOK_SECRET")],
)
def test_only_the_active_platform_needs_a_secret(monkeypatch, platform, other_secret_name):
    monkeypatch.setattr(codebase_settings, "CLIENT", platform)
    monkeypatch.setattr(webhooks_settings, other_secret_name, None)

    assert check_webhook_secret(None) == []


def test_the_swe_client_needs_no_secret(monkeypatch):
    monkeypatch.setattr(codebase_settings, "CLIENT", GitPlatform.SWE)
    monkeypatch.setattr(webhooks_settings, "GITLAB_WEBHOOK_SECRET", None)
    monkeypatch.setattr(webhooks_settings, "GITHUB_WEBHOOK_SECRET", None)

    assert check_webhook_secret(None) == []
