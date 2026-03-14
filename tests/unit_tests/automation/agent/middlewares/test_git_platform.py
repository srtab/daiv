from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from langchain.tools import ToolRuntime
from langgraph.types import Command

from automation.agent.middlewares.git_platform import github_tool, gitlab_tool
from codebase.base import GitPlatform


@patch("automation.agent.middlewares.git_platform.cache.lock", new=MagicMock())
class TestGitHubToolTokenCaching:
    async def test_github_tool_caches_token_in_state_and_reuses_it(self):
        runtime = ToolRuntime(
            state={},
            context=Mock(repo_id="owner/repo", git_platform=GitPlatform.GITHUB),
            config={"configurable": {"thread_id": "test-thread-1"}},
            stream_writer=Mock(),
            tool_call_id="test_call_1",
            store=None,
        )

        with (
            patch("automation.agent.middlewares.git_platform.get_github_integration") as get_integration_mock,
            patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc_mock,
        ):
            access_token = Mock(token="tok_1", expires_at=Mock(timestamp=Mock(return_value=9999999999.0)))  # noqa: S106
            get_integration_mock.return_value.get_access_token.return_value = access_token

            proc = Mock()
            proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
            proc.returncode = 0
            create_proc_mock.return_value = proc

            result1 = await github_tool.coroutine(subcommand="issue view 1", runtime=runtime)
            # Handle Command return - extract output and apply state update
            if isinstance(result1, Command):
                assert result1.update is not None
                # Extract output from ToolMessage in messages
                messages = result1.update.get("messages", [])
                assert len(messages) == 1
                out1 = messages[0].content
                # Apply state updates (excluding messages)
                state_updates = {k: v for k, v in result1.update.items() if k != "messages"}
                runtime.state.update(state_updates)
            else:
                out1 = result1

            result2 = await github_tool.coroutine(subcommand="issue view 2", runtime=runtime)
            # Handle Command return - extract output and apply state update
            if isinstance(result2, Command):
                assert result2.update is not None
                # Extract output from ToolMessage in messages
                messages = result2.update.get("messages", [])
                assert len(messages) == 1
                out2 = messages[0].content
                # Apply state updates (excluding messages)
                state_updates = {k: v for k, v in result2.update.items() if k != "messages"}
                runtime.state.update(state_updates)
            else:
                out2 = result2

        assert out1 == "ok"
        assert out2 == "ok"

        # Cached token should avoid extra token generation.
        assert get_integration_mock.return_value.get_access_token.call_count == 1
        assert runtime.state["github_token"] == "tok_1"  # noqa: S105
        assert runtime.state["github_token_expires_at"] is not None

    async def test_github_tool_refreshes_token_after_cache_ttl(self):
        runtime = ToolRuntime(
            state={"github_token": "tok_old", "github_token_expires_at": 0.0},
            context=Mock(repo_id="owner/repo", git_platform=GitPlatform.GITHUB),
            config={"configurable": {"thread_id": "test-thread-2"}},
            stream_writer=Mock(),
            tool_call_id="test_call_2",
            store=None,
        )

        with (
            patch("automation.agent.middlewares.git_platform.get_github_integration") as get_integration_mock,
            patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc_mock,
        ):
            access_token = Mock(token="tok_new", expires_at=Mock(timestamp=Mock(return_value=9999999999.0)))  # noqa: S106
            get_integration_mock.return_value.get_access_token.return_value = access_token

            proc = Mock()
            proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
            proc.returncode = 0
            create_proc_mock.return_value = proc

            result = await github_tool.coroutine(subcommand="issue view 1", runtime=runtime)
            # Handle Command return - apply state update
            if isinstance(result, Command) and result.update is not None:
                # Apply state updates (excluding messages)
                state_updates = {k: v for k, v in result.update.items() if k != "messages"}
                runtime.state.update(state_updates)

        assert get_integration_mock.return_value.get_access_token.call_count == 1
        assert runtime.state["github_token"] == "tok_new"  # noqa: S105

    async def test_token_not_in_tool_output(self):
        runtime = ToolRuntime(
            state={},
            context=Mock(repo_id="owner/repo", git_platform=GitPlatform.GITHUB),
            config={"configurable": {"thread_id": "test-thread-3"}},
            stream_writer=Mock(),
            tool_call_id="test_call_3",
            store=None,
        )

        with (
            patch("automation.agent.middlewares.git_platform.get_github_integration") as get_integration_mock,
            patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc_mock,
        ):
            access_token = Mock(token="tok_1", expires_at=Mock(timestamp=Mock(return_value=9999999999.0)))  # noqa: S106
            get_integration_mock.return_value.get_access_token.return_value = access_token

            proc = Mock()
            proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
            proc.returncode = 0
            create_proc_mock.return_value = proc

            result = await github_tool.coroutine(subcommand="issue view 1", runtime=runtime)
            # Handle Command return - extract output
            if isinstance(result, Command):
                assert result.update is not None
                # Extract output from ToolMessage in messages
                messages = result.update.get("messages", [])
                assert len(messages) == 1
                out = messages[0].content
            else:
                out = result

        assert out == "ok"
        assert "tok_1" not in out


def _make_gitlab_runtime(repo_slug: str = "group/repo") -> ToolRuntime:
    return ToolRuntime(
        state={},
        context=Mock(repository=Mock(slug=repo_slug), git_platform=GitPlatform.GITLAB),
        config={},
        stream_writer=Mock(),
        tool_call_id="test_call_gitlab",
        store=None,
    )


VALID_POSITION = {
    "position_type": "text",
    "base_sha": "aaa",
    "start_sha": "bbb",
    "head_sha": "ccc",
    "old_path": "src/foo.py",
    "new_path": "src/foo.py",
    "new_line": 42,
}


class TestGitLabToolInlineDiscussionFallback:
    """Tests for the python-gitlab CLI workaround that routes inline MR diff discussion
    creation through the RepoClient Python API when --position is supplied."""

    async def test_uses_python_api_when_position_flag_present(self):
        runtime = _make_gitlab_runtime()

        with patch("automation.agent.middlewares.git_platform.RepoClient") as mock_rc:
            mock_rc.create_instance.return_value.create_merge_request_inline_discussion.return_value = "disc-1"

            position_json = json.dumps(VALID_POSITION)
            result = await gitlab_tool.coroutine(
                subcommand=f'project-merge-request-discussion create --mr-iid 10 --body "nice" '
                f"--position {json.dumps(position_json)}",
                runtime=runtime,
            )

        assert isinstance(result, str)
        data = json.loads(result)
        assert data["id"] == "disc-1"
        assert data["status"] == "created"
        mock_rc.create_instance.return_value.create_merge_request_inline_discussion.assert_called_once_with(
            "group/repo", 10, "nice", VALID_POSITION
        )

    async def test_uses_python_api_with_position_equals_syntax(self):
        """--position=<value> form must also trigger the fallback.

        Single-quote shell quoting in the subcommand string ensures shlex.split
        keeps the whole JSON value (including spaces) as one token.
        """
        runtime = _make_gitlab_runtime()
        position_json = json.dumps(VALID_POSITION)
        # Wrap with shell single-quotes so shlex.split preserves the JSON as one token.
        subcommand = f"project-merge-request-discussion create --mr-iid 20 --body body '--position={position_json}'"

        with patch("automation.agent.middlewares.git_platform.RepoClient") as mock_rc:
            mock_rc.create_instance.return_value.create_merge_request_inline_discussion.return_value = "disc-eq"

            result = await gitlab_tool.coroutine(subcommand=subcommand, runtime=runtime)

        assert json.loads(result)["id"] == "disc-eq"

    async def test_falls_through_to_cli_when_no_position_flag(self):
        """Without --position the CLI subprocess must still be invoked."""
        runtime = _make_gitlab_runtime()

        mock_settings = Mock()
        mock_settings.GITLAB_AUTH_TOKEN.get_secret_value.return_value = "test-token"  # noqa: S106
        mock_settings.GITLAB_URL.encoded_string.return_value = "https://gitlab.com"

        with (
            patch("automation.agent.middlewares.git_platform.RepoClient") as mock_rc,
            patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc,
            patch("automation.agent.middlewares.git_platform.settings", mock_settings),
        ):
            proc = Mock()
            proc.communicate = AsyncMock(return_value=(b"cli-output\n", b""))
            proc.returncode = 0
            create_proc.return_value = proc

            result = await gitlab_tool.coroutine(
                subcommand='project-merge-request-discussion create --mr-iid 10 --body "hi"', runtime=runtime
            )

        assert result == "cli-output"
        mock_rc.create_instance.return_value.create_merge_request_inline_discussion.assert_not_called()
        create_proc.assert_called_once()

    async def test_error_when_mr_iid_missing(self):
        runtime = _make_gitlab_runtime()
        position_json = json.dumps(VALID_POSITION)

        with patch("automation.agent.middlewares.git_platform.RepoClient"):
            result = await gitlab_tool.coroutine(
                subcommand=f'project-merge-request-discussion create --body "b" --position {json.dumps(position_json)}',
                runtime=runtime,
            )

        assert result.startswith("error:")
        assert "--mr-iid" in result

    async def test_error_when_body_missing(self):
        runtime = _make_gitlab_runtime()
        position_json = json.dumps(VALID_POSITION)

        with patch("automation.agent.middlewares.git_platform.RepoClient"):
            result = await gitlab_tool.coroutine(
                subcommand=f"project-merge-request-discussion create --mr-iid 5 --position {json.dumps(position_json)}",
                runtime=runtime,
            )

        assert result.startswith("error:")
        assert "--body" in result

    async def test_error_when_position_is_invalid_json(self):
        runtime = _make_gitlab_runtime()

        with patch("automation.agent.middlewares.git_platform.RepoClient"):
            result = await gitlab_tool.coroutine(
                subcommand='project-merge-request-discussion create --mr-iid 5 --body "b" --position "not-json"',
                runtime=runtime,
            )

        assert result.startswith("error:")
        assert "--position" in result

    async def test_error_when_position_is_not_an_object(self):
        runtime = _make_gitlab_runtime()

        with patch("automation.agent.middlewares.git_platform.RepoClient"):
            result = await gitlab_tool.coroutine(
                subcommand='project-merge-request-discussion create --mr-iid 5 --body "b" --position "[1,2,3]"',
                runtime=runtime,
            )

        assert result.startswith("error:")

    async def test_error_propagated_from_repo_client(self):
        runtime = _make_gitlab_runtime()
        position_json = json.dumps(VALID_POSITION)

        with patch("automation.agent.middlewares.git_platform.RepoClient") as mock_rc:
            mock_rc.create_instance.return_value.create_merge_request_inline_discussion.side_effect = RuntimeError(
                "GitLab 422"
            )

            result = await gitlab_tool.coroutine(
                subcommand=f'project-merge-request-discussion create --mr-iid 10 --body "b" '
                f"--position {json.dumps(position_json)}",
                runtime=runtime,
            )

        assert result.startswith("error:")
        assert "GitLab 422" in result

    @pytest.mark.parametrize(
        "subcommand",
        [
            pytest.param(
                'project-merge-request-discussion create --mr-iid abc --body "b" --position "{}"', id="non-int-iid"
            )
        ],
    )
    async def test_error_when_mr_iid_not_integer(self, subcommand):
        runtime = _make_gitlab_runtime()

        with patch("automation.agent.middlewares.git_platform.RepoClient"):
            result = await gitlab_tool.coroutine(subcommand=subcommand, runtime=runtime)

        assert result.startswith("error:")
        assert "--mr-iid" in result
