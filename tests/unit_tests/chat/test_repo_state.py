from unittest.mock import MagicMock, patch

import pytest

from chat.repo_state import aget_existing_mr_payload, mr_to_payload
from codebase.base import MergeRequest
from codebase.base import User as CBUser


def _make_mr(**overrides):
    base = {
        "repo_id": "a/b",
        "merge_request_id": 42,
        "source_branch": "feature-x",
        "target_branch": "main",
        "title": "Add feature X",
        "description": "",
        "labels": [],
        "web_url": "https://gitlab.example/a/b/-/merge_requests/42",
        "sha": "deadbeef",
        "author": CBUser(id=1, username="u", name="U"),
        "draft": True,
    }
    base.update(overrides)
    return MergeRequest(**base)


async def test_returns_none_when_repo_id_missing():
    with patch("chat.repo_state.RepositoryConfig.get_config") as get_config:
        result = await aget_existing_mr_payload("", "feature-x")
    assert result is None
    get_config.assert_not_called()


async def test_returns_none_when_ref_missing():
    with patch("chat.repo_state.RepositoryConfig.get_config") as get_config:
        result = await aget_existing_mr_payload("a/b", "")
    assert result is None
    get_config.assert_not_called()


async def test_returns_none_when_ref_is_default_branch_and_skips_lookup():
    repo_client = MagicMock()
    with (
        patch("chat.repo_state.RepositoryConfig.get_config", return_value=MagicMock(default_branch="main")),
        patch("chat.repo_state.RepoClient.create_instance", return_value=repo_client) as factory,
    ):
        result = await aget_existing_mr_payload("a/b", "main")

    assert result is None
    factory.assert_not_called()
    repo_client.get_merge_request_by_branches.assert_not_called()


async def test_returns_payload_on_happy_path():
    repo_client = MagicMock()
    repo_client.get_merge_request_by_branches.return_value = _make_mr()
    with (
        patch("chat.repo_state.RepositoryConfig.get_config", return_value=MagicMock(default_branch="main")),
        patch("chat.repo_state.RepoClient.create_instance", return_value=repo_client),
    ):
        result = await aget_existing_mr_payload("a/b", "feature-x")

    assert result == mr_to_payload(_make_mr())
    assert result["id"] == 42
    assert result["draft"] is True
    repo_client.get_merge_request_by_branches.assert_called_once_with("a/b", "feature-x", "main")


async def test_returns_none_when_lookup_returns_none():
    repo_client = MagicMock()
    repo_client.get_merge_request_by_branches.return_value = None
    with (
        patch("chat.repo_state.RepositoryConfig.get_config", return_value=MagicMock(default_branch="main")),
        patch("chat.repo_state.RepoClient.create_instance", return_value=repo_client),
    ):
        result = await aget_existing_mr_payload("a/b", "feature-x")

    assert result is None


async def test_swallows_platform_errors_and_logs(caplog):
    """Platform hiccups (HTTP/transport errors) degrade to None with a logged
    exception. Programming bugs are NOT swallowed — they propagate.
    """
    import httpx

    with (
        patch("chat.repo_state.RepositoryConfig.get_config", side_effect=httpx.ConnectError("platform unreachable")),
        caplog.at_level("ERROR", logger="daiv.chat"),
    ):
        result = await aget_existing_mr_payload("a/b", "feature-x")

    assert result is None
    assert any("Failed to look up existing merge request" in rec.message for rec in caplog.records)


async def test_swallows_errors_from_client_call():
    """SDK errors (gitlab/github/httpx) are caught."""
    from gitlab.exceptions import GitlabError

    repo_client = MagicMock()
    repo_client.get_merge_request_by_branches.side_effect = GitlabError("api 500")
    with (
        patch("chat.repo_state.RepositoryConfig.get_config", return_value=MagicMock(default_branch="main")),
        patch("chat.repo_state.RepoClient.create_instance", return_value=repo_client),
    ):
        result = await aget_existing_mr_payload("a/b", "feature-x")

    assert result is None


async def test_propagates_unexpected_errors():
    """Bugs (KeyError/AttributeError/TypeError) must NOT be silently caught —
    they should surface as 500s rather than masking as a fake 'no MR'.
    """
    repo_client = MagicMock()
    repo_client.get_merge_request_by_branches.side_effect = KeyError("missing field")
    with (
        patch("chat.repo_state.RepositoryConfig.get_config", return_value=MagicMock(default_branch="main")),
        patch("chat.repo_state.RepoClient.create_instance", return_value=repo_client),
        pytest.raises(KeyError),
    ):
        await aget_existing_mr_payload("a/b", "feature-x")


@pytest.mark.parametrize(("payload_input", "expected_keys"), [(None, None), ("not-a-mr", None)])
def test_mr_to_payload_handles_invalid_inputs(payload_input, expected_keys):
    assert mr_to_payload(payload_input) is expected_keys


def test_mr_to_payload_accepts_dict_form():
    """Re-hydrated checkpointer state stores MRs as dicts; payload converter must accept that shape."""
    raw = {
        "merge_request_id": 7,
        "web_url": "https://x/7",
        "title": "T",
        "draft": True,
        "source_branch": "f",
        "target_branch": "m",
    }
    payload = mr_to_payload(raw)
    assert payload == {
        "id": 7,
        "url": "https://x/7",
        "title": "T",
        "draft": True,
        "source_branch": "f",
        "target_branch": "m",
    }
