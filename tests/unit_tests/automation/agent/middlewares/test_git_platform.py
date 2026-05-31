from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from langchain.tools import ToolRuntime
from langgraph.types import Command

from automation.agent.middlewares.git_platform import (
    GITHUB_TOOL_DESCRIPTION,
    GITLAB_TOOL_DESCRIPTION,
    _exceeds_output_cap,
    _finalize_inline_output,
    _handle_output_redirect,
    _redirect_confirmation,
    _validate_workspace_path,
    _write_output_to_sandbox,
    github_tool,
    gitlab_tool,
)
from codebase.base import GitPlatform
from core.sandbox.schemas import FsWriteResponse


@contextmanager
def _mock_sandbox_client(*, ok: bool = True, error: str | None = None):
    """Patch DAIVSandboxClient with an async mock; yield the client mock for assertions."""
    client = Mock()
    client.open = AsyncMock()
    client.close = AsyncMock()
    client.fs_write = AsyncMock(return_value=FsWriteResponse(ok=ok, error=error))
    with patch("automation.agent.middlewares.git_platform.DAIVSandboxClient", return_value=client):
        yield client


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


@pytest.mark.parametrize("path", ["/workspace/tmp/x.json", "/workspace/repo/a/b.txt", "/workspace/out"])
def test_validate_workspace_path_accepts_under_workspace(path):
    assert _validate_workspace_path(path) is None


@pytest.mark.parametrize("path", ["relative.json", "/etc/passwd", "/workspace/../etc/passwd", "/workspace", ""])
def test_validate_workspace_path_rejects(path):
    err = _validate_workspace_path(path)
    assert err is not None
    assert err.startswith("error:")


def test_exceeds_output_cap_thresholds():
    assert _exceeds_output_cap("\n".join(str(i) for i in range(2100))) is True  # 2100 lines
    assert _exceeds_output_cap("\n".join(str(i) for i in range(100))) is False  # 100 lines
    assert _exceeds_output_cap("\n".join(str(i) for i in range(2000))) is False  # exactly 2000 lines


def test_redirect_confirmation_shape():
    output = "line1\nline2\nline3"
    msg = _redirect_confirmation("/workspace/tmp/x.json", 17, 3, output)
    assert "/workspace/tmp/x.json" in msg
    assert "17 bytes" in msg
    assert "3 lines" in msg
    assert "line1" in msg  # head preview included


def test_redirect_confirmation_caps_preview_to_25_lines():
    output = "\n".join(f"line{i}" for i in range(100))
    msg = _redirect_confirmation("/workspace/tmp/x.json", 999, 100, output)
    assert "line24" in msg  # 25th line (0-indexed) is shown
    assert "line25" not in msg  # 26th line is not


async def test_write_output_to_sandbox_writes_and_returns_counts():
    with _mock_sandbox_client() as client:
        byte_count, line_count = await _write_output_to_sandbox("a\nb\nc", "/workspace/tmp/x.txt", "sess-9")

    assert client.fs_write.call_args.args[0] == "sess-9"
    req = client.fs_write.call_args.args[1]
    assert req.path == "/workspace/tmp/x.txt"
    assert req.content == b"a\nb\nc"  # Base64Bytes stores decoded bytes after validation
    assert (byte_count, line_count) == (5, 3)
    client.open.assert_awaited_once()
    client.close.assert_awaited_once()


async def test_write_output_to_sandbox_raises_on_not_ok_and_still_closes():
    with _mock_sandbox_client(ok=False, error="disk full") as client, pytest.raises(RuntimeError, match="disk full"):
        await _write_output_to_sandbox("x", "/workspace/tmp/x.txt", "s")
    client.close.assert_awaited_once()


async def test_handle_redirect_degrades_inline_without_session():
    runtime = _make_gitlab_runtime()  # state has no session_id
    result = await _handle_output_redirect(
        output="hello", output_file="/workspace/tmp/x.json", runtime=runtime, keep="head", tool_name="gitlab"
    )
    assert "output_file was ignored" in result
    assert "hello" in result


async def test_handle_redirect_writes_and_confirms_with_session():
    runtime = _make_gitlab_runtime()
    runtime.state["session_id"] = "sess-1"
    with _mock_sandbox_client() as client:
        result = await _handle_output_redirect(
            output="payload-data", output_file="/workspace/tmp/x.json", runtime=runtime, keep="head", tool_name="gitlab"
        )
    assert client.fs_write.call_args.args[1].path == "/workspace/tmp/x.json"
    assert result.startswith("Wrote ")


async def test_handle_redirect_returns_error_on_write_failure():
    runtime = _make_gitlab_runtime()
    runtime.state["session_id"] = "sess-1"
    with _mock_sandbox_client(ok=False, error="boom"):
        result = await _handle_output_redirect(
            output="x", output_file="/workspace/tmp/x.json", runtime=runtime, keep="head", tool_name="gitlab"
        )
    assert result.startswith("error:")
    assert "/workspace/tmp/x.json" in result


async def test_finalize_inline_returns_plain_output_when_small():
    runtime = _make_gitlab_runtime()
    with patch("automation.agent.middlewares.git_platform.DAIVSandboxClient") as cls:
        result = await _finalize_inline_output(
            output="small", runtime=runtime, resource="r", action="a", keep="head", tool_name="gitlab"
        )
        cls.assert_not_called()
    assert result == "small"


async def test_finalize_inline_auto_evicts_when_oversized_with_session():
    runtime = _make_gitlab_runtime()
    runtime.state["session_id"] = "sess-1"
    big = "\n".join(str(i) for i in range(2100))
    with _mock_sandbox_client() as client:
        result = await _finalize_inline_output(
            output=big,
            runtime=runtime,
            resource="project-merge-request",
            action="list",
            keep="head",
            tool_name="gitlab",
        )
    path = client.fs_write.call_args.args[1].path
    assert path.startswith("/workspace/tmp/gitlab-project-merge-request-list-")
    assert path.endswith(".txt")
    assert "written verbatim to the scratch file" in result
    # Note must be platform-neutral and guide both gitlab and gh users
    assert "output_file=" in result
    assert "--json" in result


async def test_finalize_inline_truncates_without_session_when_oversized():
    runtime = _make_gitlab_runtime()  # no session
    big = "\n".join(str(i) for i in range(2100))
    with patch("automation.agent.middlewares.git_platform.DAIVSandboxClient") as cls:
        result = await _finalize_inline_output(
            output=big, runtime=runtime, resource="r", action="a", keep="head", tool_name="gitlab"
        )
        cls.assert_not_called()
    assert "truncated" in result  # sentinel from _truncate_cli_output


async def test_finalize_inline_falls_back_to_truncation_when_evict_fails():
    runtime = _make_gitlab_runtime()
    runtime.state["session_id"] = "sess-1"
    big = "\n".join(str(i) for i in range(2100))
    with _mock_sandbox_client(ok=False, error="disk full"):
        result = await _finalize_inline_output(
            output=big, runtime=runtime, resource="r", action="a", keep="head", tool_name="gitlab"
        )
    assert "truncated" in result  # fallback to _truncate_cli_output sentinel
    assert "writing it to a sandbox scratch file failed" in result  # eviction-failure note


async def test_gitlab_output_file_forces_json_writes_and_confirms():
    runtime = _make_gitlab_runtime()
    runtime.state["session_id"] = "sess-1"

    mock_settings = Mock()
    mock_settings.GITLAB_AUTH_TOKEN.get_secret_value.return_value = "test-token"  # noqa: S106
    mock_settings.GITLAB_URL.encoded_string.return_value = "https://gitlab.com"

    payload = b'[{"iid": 1}, {"iid": 2}]\n'

    with (
        patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc,
        patch("automation.agent.middlewares.git_platform.settings", mock_settings),
        _mock_sandbox_client() as client,
    ):
        proc = Mock()
        proc.communicate = AsyncMock(return_value=(payload, b""))
        proc.returncode = 0
        create_proc.return_value = proc

        result = await gitlab_tool.coroutine(
            subcommand="project-merge-request list --state opened",
            runtime=runtime,
            output_mode="detailed",
            output_file="/workspace/tmp/mrs.json",
        )

    argv = list(create_proc.call_args.args)
    assert "--output" in argv and argv[argv.index("--output") + 1] == "json"
    assert "--verbose" not in argv  # output_mode ignored on redirect

    assert client.fs_write.call_args.args[0] == "sess-1"
    req = client.fs_write.call_args.args[1]
    assert req.path == "/workspace/tmp/mrs.json"
    assert req.content == b'[{"iid": 1}, {"iid": 2}]'  # full, untruncated, decoded
    assert result.startswith("Wrote ")
    assert "/workspace/tmp/mrs.json" in result
    assert '"iid": 1' in result  # head preview


async def test_gitlab_output_file_does_not_force_json_for_job_trace():
    runtime = _make_gitlab_runtime()
    runtime.state["session_id"] = "sess-1"
    mock_settings = Mock()
    mock_settings.GITLAB_AUTH_TOKEN.get_secret_value.return_value = "test-token"  # noqa: S106
    mock_settings.GITLAB_URL.encoded_string.return_value = "https://gitlab.com"
    with (
        patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc,
        patch("automation.agent.middlewares.git_platform.settings", mock_settings),
        _mock_sandbox_client() as client,
    ):
        proc = Mock()
        proc.communicate = AsyncMock(return_value=(b"log line 1\nlog line 2\n", b""))
        proc.returncode = 0
        create_proc.return_value = proc
        result = await gitlab_tool.coroutine(
            subcommand="project-job trace --id 55", runtime=runtime, output_file="/workspace/tmp/trace.txt"
        )
    argv = list(create_proc.call_args.args)
    assert "--output" not in argv  # traces are raw log text; JSON would be degenerate
    assert client.fs_write.call_args.args[1].path == "/workspace/tmp/trace.txt"
    assert result.startswith("Wrote ")


async def test_gitlab_invalid_output_file_errors_before_running_cli():
    runtime = _make_gitlab_runtime()
    runtime.state["session_id"] = "sess-1"
    with (
        patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc,
        patch("automation.agent.middlewares.git_platform.DAIVSandboxClient") as client_cls,
    ):
        result = await gitlab_tool.coroutine(
            subcommand="project-issue get --iid 1", runtime=runtime, output_file="/etc/passwd"
        )
    assert result.startswith("error:")
    create_proc.assert_not_called()
    client_cls.assert_not_called()


@patch("automation.agent.middlewares.git_platform.cache.lock", new=MagicMock())
class TestGitHubToolOutputFile:
    async def test_github_output_file_writes_verbatim_and_wraps_in_command(self):
        runtime = ToolRuntime(
            state={"session_id": "sess-1"},
            context=Mock(repo_id="owner/repo", git_platform=GitPlatform.GITHUB),
            config={"configurable": {"thread_id": "t-gh-of"}},
            stream_writer=Mock(),
            tool_call_id="c1",
            store=None,
        )
        payload = b'{"number": 7}\n'

        with (
            patch("automation.agent.middlewares.git_platform.get_github_integration") as gi,
            patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc,
            _mock_sandbox_client() as client,
        ):
            gi.return_value.get_access_token.return_value = Mock(
                token="tok",  # noqa: S106
                expires_at=Mock(timestamp=Mock(return_value=9999999999.0)),
            )
            proc = Mock()
            proc.communicate = AsyncMock(return_value=(payload, b""))
            proc.returncode = 0
            create_proc.return_value = proc

            result = await github_tool.coroutine(
                subcommand="pr view 7 --json number", runtime=runtime, output_file="/workspace/tmp/pr.json"
            )

        # gh is written verbatim — the tool never injects a global --output flag
        argv = list(create_proc.call_args.args)
        assert "--output" not in argv

        req = client.fs_write.call_args.args[1]
        assert req.path == "/workspace/tmp/pr.json"
        assert req.content == b'{"number": 7}'

        # token was refreshed → Command, and its ToolMessage carries the confirmation
        assert isinstance(result, Command)
        msg = result.update["messages"][0].content
        assert msg.startswith("Wrote ")
        assert "/workspace/tmp/pr.json" in msg

    async def test_github_invalid_output_file_errors_before_running_cli(self):
        runtime = ToolRuntime(
            state={"session_id": "sess-1"},
            context=Mock(repo_id="owner/repo", git_platform=GitPlatform.GITHUB),
            config={"configurable": {"thread_id": "t-gh-bad"}},
            stream_writer=Mock(),
            tool_call_id="c2",
            store=None,
        )
        with (
            patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc,
            patch("automation.agent.middlewares.git_platform.DAIVSandboxClient") as client_cls,
        ):
            result = await github_tool.coroutine(
                subcommand="issue view 1", runtime=runtime, output_file="../escape.json"
            )
        assert result.startswith("error:")
        create_proc.assert_not_called()
        client_cls.assert_not_called()

    async def test_github_empty_output_with_output_file_notes_file_not_written(self):
        """When gh returns empty stdout and output_file is set, the result must contain
        both the 'empty result' sentinel and a note that the file was not written."""
        runtime = ToolRuntime(
            state={"session_id": "sess-1", "github_token": "tok", "github_token_expires_at": 9999999999.0},
            context=Mock(repo_id="owner/repo", git_platform=GitPlatform.GITHUB),
            config={"configurable": {"thread_id": "t-gh-empty"}},
            stream_writer=Mock(),
            tool_call_id="c3",
            store=None,
        )
        with (
            patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc,
            patch("automation.agent.middlewares.git_platform.DAIVSandboxClient") as client_cls,
        ):
            proc = Mock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = 0
            create_proc.return_value = proc

            result = await github_tool.coroutine(
                subcommand="issue list --state open", runtime=runtime, output_file="/workspace/tmp/issues.json"
            )

        # Token was already cached → plain string (no Command)
        assert isinstance(result, str)
        assert "empty result" in result
        assert "not written" in result
        client_cls.assert_not_called()


async def test_gitlab_empty_output_with_output_file_notes_file_not_written():
    """When the gitlab CLI returns empty stdout and output_file is set, the result must
    contain both the 'empty result' sentinel and a note that the file was not written."""
    runtime = _make_gitlab_runtime()
    runtime.state["session_id"] = "sess-1"

    mock_settings = Mock()
    mock_settings.GITLAB_AUTH_TOKEN.get_secret_value.return_value = "test-token"  # noqa: S106
    mock_settings.GITLAB_URL.encoded_string.return_value = "https://gitlab.com"

    with (
        patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc,
        patch("automation.agent.middlewares.git_platform.settings", mock_settings),
        patch("automation.agent.middlewares.git_platform.DAIVSandboxClient") as client_cls,
    ):
        proc = Mock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        create_proc.return_value = proc

        result = await gitlab_tool.coroutine(
            subcommand="project-issue list --state opened", runtime=runtime, output_file="/workspace/tmp/issues.json"
        )

    assert "empty result" in result
    assert "not written" in result
    client_cls.assert_not_called()


def test_tool_descriptions_document_output_file():
    for desc in (GITLAB_TOOL_DESCRIPTION, GITHUB_TOOL_DESCRIPTION):
        assert "output_file" in desc
        assert "/workspace" in desc
        assert "/workspace/tmp" in desc  # transient-dump guidance
    assert "--output json" in GITLAB_TOOL_DESCRIPTION  # gitlab forces JSON on redirect
    assert "--json" in GITHUB_TOOL_DESCRIPTION  # gh opts into JSON via its own flag


def test_tool_descriptions_document_project_job_trace_raw_text():
    """GITLAB_TOOL_DESCRIPTION must mention project-job trace together with raw log text."""
    assert "project-job trace" in GITLAB_TOOL_DESCRIPTION
    assert "raw log text" in GITLAB_TOOL_DESCRIPTION


def test_redirect_confirmation_caps_preview_chars():
    output = "x" * 5000  # single very long line
    msg = _redirect_confirmation("/workspace/tmp/x.json", 5000, 1, output)
    assert "(preview truncated)" in msg
    assert len(msg) < 5000


@patch("automation.agent.middlewares.git_platform.cache.lock", new=MagicMock())
class TestGitHubToolOutputFileExtra:
    async def test_github_run_view_log_redirect(self):
        """gh run view --log redirect: clean_job_logs + keep='tail'; file written, confirmation returned."""
        runtime = ToolRuntime(
            state={"session_id": "sess-2", "github_token": "tok", "github_token_expires_at": 9999999999.0},
            context=Mock(repo_id="owner/repo", git_platform=GitPlatform.GITHUB),
            config={"configurable": {"thread_id": "t-gh-log"}},
            stream_writer=Mock(),
            tool_call_id="c-log",
            store=None,
        )
        log_output = b"2024-01-01T00:00:00.000Z job1\tsome log line\n"

        with (
            patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc,
            patch(
                "automation.agent.middlewares.git_platform.clean_job_logs", return_value="some log line"
            ) as mock_clean,
            _mock_sandbox_client() as client,
        ):
            proc = Mock()
            proc.communicate = AsyncMock(return_value=(log_output, b""))
            proc.returncode = 0
            create_proc.return_value = proc

            result = await github_tool.coroutine(
                subcommand="run view 123 --job 456 --log", runtime=runtime, output_file="/workspace/tmp/log.txt"
            )

        # Token was already cached → plain string (no Command)
        assert isinstance(result, str)
        assert result.startswith("Wrote ")
        assert client.fs_write.call_args.args[1].path == "/workspace/tmp/log.txt"
        mock_clean.assert_called_once()

    async def test_github_output_file_cached_token_plain_string(self):
        """gh output_file with a cached valid token returns a plain str (no Command)."""
        runtime = ToolRuntime(
            state={"session_id": "sess-3", "github_token": "tok", "github_token_expires_at": 9999999999.0},
            context=Mock(repo_id="owner/repo", git_platform=GitPlatform.GITHUB),
            config={"configurable": {"thread_id": "t-gh-cached"}},
            stream_writer=Mock(),
            tool_call_id="c-cached",
            store=None,
        )
        payload = b'{"number": 7}\n'

        with (
            patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc,
            _mock_sandbox_client(),
        ):
            proc = Mock()
            proc.communicate = AsyncMock(return_value=(payload, b""))
            proc.returncode = 0
            create_proc.return_value = proc

            result = await github_tool.coroutine(
                subcommand="pr view 7 --json number", runtime=runtime, output_file="/workspace/tmp/pr.json"
            )

        assert isinstance(result, str)
        assert result.startswith("Wrote ")
