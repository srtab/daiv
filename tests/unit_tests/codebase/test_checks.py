from unittest.mock import patch

import pytest
from pydantic import SecretStr

from codebase.base import GitPlatform
from codebase.checks import check_api_keys


def _run(**overrides):
    """Run the codebase system check with patched settings."""
    from codebase.conf import settings

    defaults = {
        "CLIENT": GitPlatform.GITLAB,
        "GITLAB_AUTH_TOKEN": SecretStr("token"),
        "GITLAB_WEBHOOK_SECRET": SecretStr("secret"),
        "GITHUB_PRIVATE_KEY": SecretStr("key"),
        "GITHUB_APP_ID": 1,
        "GITHUB_INSTALLATION_ID": 1,
        "GITHUB_WEBHOOK_SECRET": SecretStr("secret"),
    }
    defaults.update(overrides)
    with patch.multiple(settings, **defaults):
        return check_api_keys(None)


@pytest.mark.parametrize(
    "client,secret_attr,missing_env",
    [
        (GitPlatform.GITLAB, "GITLAB_WEBHOOK_SECRET", "CODEBASE_GITLAB_WEBHOOK_SECRET"),
        (GitPlatform.GITHUB, "GITHUB_WEBHOOK_SECRET", "CODEBASE_GITHUB_WEBHOOK_SECRET"),
    ],
)
def test_check_flags_missing_webhook_secret_for_active_platform(client, secret_attr, missing_env):
    """The active platform's webhook secret is required — fail closed needs a secret to exist."""
    errors = _run(CLIENT=client, **{secret_attr: None})
    messages = " ".join(e.msg for e in errors)
    assert "webhook secret" in messages
    assert missing_env in messages


def test_check_no_error_when_gitlab_secret_configured():
    assert _run(CLIENT=GitPlatform.GITLAB) == []


def test_check_no_error_when_github_secret_configured():
    assert _run(CLIENT=GitPlatform.GITHUB, GITLAB_AUTH_TOKEN=None, GITLAB_WEBHOOK_SECRET=None) == []


def test_check_swe_client_requires_no_secrets():
    # SWE client carries no API keys and no webhook secret.
    assert (
        _run(
            CLIENT=GitPlatform.SWE,
            GITLAB_AUTH_TOKEN=None,
            GITLAB_WEBHOOK_SECRET=None,
            GITHUB_PRIVATE_KEY=None,
            GITHUB_APP_ID=None,
            GITHUB_INSTALLATION_ID=None,
            GITHUB_WEBHOOK_SECRET=None,
        )
        == []
    )


def test_check_github_secret_not_flagged_when_gitlab_active():
    """A missing GitHub secret must not error while GitLab is the active platform."""
    errors = _run(CLIENT=GitPlatform.GITLAB, GITHUB_WEBHOOK_SECRET=None)
    assert errors == []
