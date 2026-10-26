import base64
from unittest.mock import MagicMock

import pytest
import requests
from github import GithubException
from gitlab.exceptions import GitlabError
from pydantic import SecretStr

from codebase.base import GitPlatform, Repository
from codebase.clients.base import GitAuthEnv, GitEgressCredential, RepoClient, aget_open_merge_request


def test_git_auth_env_as_env_builds_host_scoped_extraheader_config():
    """as_env() must authenticate git-over-HTTPS without the token ever reaching argv or .git/config:
    a command-scoped http.<origin>.extraheader plus fully disabled prompting (so a *rejected*
    credential fails fast with git's 'could not read Username' auth marker instead of hanging).
    Both GIT_TERMINAL_PROMPT=0 and GIT_ASKPASS='' are required: the empty GIT_ASKPASS short-circuits
    git's fallback to an inherited SSH_ASKPASS GUI helper, which TERMINAL_PROMPT alone does not."""
    env = GitAuthEnv.for_token("https://gitlab.com/group/repo.git", "tok-123").as_env()

    assert env == {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://gitlab.com/.extraheader",
        "GIT_CONFIG_VALUE_0": "Authorization: Basic " + base64.b64encode(b"oauth2:tok-123").decode(),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
    }


def test_git_auth_env_preserves_scheme_and_port():
    """git matches http.<url>.* config by URL prefix — dropping a non-default port (or the scheme)
    would silently never match, leaving every request unauthenticated."""
    env = GitAuthEnv.for_token("http://gitlab.local:8080/group/repo.git", "tok").as_env()

    assert env["GIT_CONFIG_KEY_0"] == "http.http://gitlab.local:8080/.extraheader"


def test_git_auth_env_holds_header_as_secret():
    """The credential must be wrapped in SecretStr so it never appears verbatim in a repr / log /
    Sentry stack-local — the same protection GitEgressCredential uses. Only as_env() (called at the
    subprocess/clone boundary) materialises the plaintext."""
    auth = GitAuthEnv.for_token("https://gitlab.com/group/repo.git", "tok-123")

    assert isinstance(auth.header, SecretStr)
    assert "tok-123" not in repr(auth)
    assert "oauth2" not in repr(auth)
    # But the real value is recoverable at the boundary.
    assert (
        "oauth2:tok-123"
        in base64.b64decode(auth.as_env()["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: Basic ")).decode()
    )


def test_repo_client_declares_member_listing():
    assert "list_repository_members" in RepoClient.__abstractmethods__


async def test_aget_open_merge_request_returns_the_platform_answer():
    mr = MagicMock()
    client = MagicMock(get_merge_request_by_branches=MagicMock(return_value=mr))

    assert await aget_open_merge_request(client, "a/b", "feature-x") is mr
    client.get_merge_request_by_branches.assert_called_once_with("a/b", "feature-x")


@pytest.mark.parametrize(
    "error",
    [GitlabError("gitlab down"), GithubException(502), requests.ConnectionError("dns failure")],
    ids=["gitlab", "github", "transport"],
)
async def test_aget_open_merge_request_reads_a_platform_failure_as_no_merge_request(error):
    client = MagicMock(get_merge_request_by_branches=MagicMock(side_effect=error))

    assert await aget_open_merge_request(client, "a/b", "feature-x") is None


async def test_aget_open_merge_request_propagates_bugs():
    """A bug read as "no MR" would have the publisher open a duplicate one."""
    client = MagicMock(get_merge_request_by_branches=MagicMock(side_effect=KeyError("missing field")))

    with pytest.raises(KeyError):
        await aget_open_merge_request(client, "a/b", "feature-x")


def test_git_egress_credential_carries_clone_url_path():
    repository = Repository(
        pk=1,
        slug="g/sub/app",
        name="app",
        clone_url="https://gitlab.com/g/sub/app.git",
        html_url="https://gitlab.com/g/sub/app",
        default_branch="main",
        git_platform=GitPlatform.GITLAB,
    )
    client = MagicMock(spec=RepoClient)
    client._git_egress_token.return_value = "tok"

    credential = RepoClient.get_git_egress_credential(client, repository)

    assert credential.host == "gitlab.com"
    assert credential.path == "/g/sub/app.git"
    assert GitEgressCredential.for_token(host="h", token=None).path == ""
