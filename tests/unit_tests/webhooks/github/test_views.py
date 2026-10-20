import hmac
import json
from hashlib import sha256
from unittest.mock import patch

import pytest
from ninja.testing import TestAsyncClient
from webhooks.conf import settings as webhooks_settings
from webhooks.github.callbacks import IssueCallback

from codebase.base import GitPlatform
from codebase.conf import settings as codebase_settings
from daiv.api import api

ISSUE_OPENED = json.dumps({
    "action": "opened",
    "repository": {"id": 1, "full_name": "owner/repo", "default_branch": "main"},
    "issue": {"id": 100, "number": 42, "title": "Test Issue", "state": "open", "labels": []},
    "sender": {"id": 10, "login": "alice"},
}).encode()


def _sign(body: bytes, key: bytes = b"test_secret") -> str:
    return "sha256=" + hmac.new(key, msg=body, digestmod=sha256).hexdigest()


@pytest.fixture(autouse=True)
def github_platform(monkeypatch):
    monkeypatch.setattr(codebase_settings, "CLIENT", GitPlatform.GITHUB)


@pytest.fixture
def client():
    return TestAsyncClient(api)


@pytest.fixture
def platform_calls():
    with (
        patch("webhooks.github.callbacks.RepositoryConfig.get_config") as get_config,
        patch("webhooks.github.callbacks.RepoClient.create_instance") as create_instance,
    ):
        yield get_config, create_instance


async def _post(client: TestAsyncClient, body: bytes, headers: dict[str, str] | None = None):
    return await client.post(
        "/codebase/callbacks/github/", data=body, headers={"X-GitHub-Event": "issues", **(headers or {})}
    )


async def test_a_signed_request_is_processed(client: TestAsyncClient):
    with (
        patch.object(IssueCallback, "accept_callback", return_value=True) as accept_callback,
        patch.object(IssueCallback, "process_callback") as process_callback,
    ):
        response = await _post(client, ISSUE_OPENED, {"X-Hub-Signature-256": _sign(ISSUE_OPENED)})

    assert response.status_code == 204
    accept_callback.assert_called_once()
    process_callback.assert_awaited_once()


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="unsigned"),
        pytest.param({"X-Hub-Signature-256": "sha256=" + "0" * 64}, id="wrong-signature"),
        pytest.param({"X-Hub-Signature-256": _sign(ISSUE_OPENED, key=b"other")}, id="other-secret"),
    ],
)
async def test_an_unverified_request_is_rejected_before_the_payload_is_parsed(
    client: TestAsyncClient, platform_calls, headers
):
    response = await _post(client, ISSUE_OPENED, headers)

    assert response.status_code == 401
    for platform_call in platform_calls:
        platform_call.assert_not_called()


async def test_an_unsigned_malformed_payload_is_401_not_422(client: TestAsyncClient):
    """A 422 would mean the body was parsed, and its error detail leaked, before the signature was checked."""
    response = await _post(client, b'{"action": "opened"}')

    assert response.status_code == 401


async def test_without_a_configured_secret_every_request_is_rejected(
    client: TestAsyncClient, platform_calls, monkeypatch
):
    monkeypatch.setattr(webhooks_settings, "GITHUB_WEBHOOK_SECRET", None)

    response = await _post(client, ISSUE_OPENED, {"X-Hub-Signature-256": _sign(ISSUE_OPENED)})

    assert response.status_code == 401
    for platform_call in platform_calls:
        platform_call.assert_not_called()
