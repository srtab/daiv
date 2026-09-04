from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from langchain.tools import ToolRuntime
from langgraph.types import Command

from automation.agent.middlewares.file_system import DAIVCompositeBackend, SandboxFileBackend
from automation.agent.middlewares.git_platform import (
    GITHUB_CLI_ALLOW_COMMANDS,
    GITHUB_TOOL_DESCRIPTION,
    GITLAB_TOOL_DESCRIPTION,
    GitPlatformMiddleware,
    _file_write_confirmation,
    _is_allowed_cli_command,
    _large_tool_results_prefix,
    _run_github_subcommand,
    _run_gitlab_subcommand,
    _write_output_to_file,
)
from codebase.base import GitPlatform

LARGE_TOOL_RESULTS_PREFIX = "/workspace/large_tool_results"


def _mock_backend(*, error: str | None = None):
    """Filesystem backend stub whose ``awrite`` records calls and returns a WriteResult-like obj."""
    backend = Mock()
    backend.awrite = AsyncMock(return_value=Mock(error=error))
    return backend


async def _run_gl(subcommand, runtime, *, output_mode="simplified", to_file=False, backend=None):
    """Invoke the gitlab tool implementation with a default mock backend + results prefix."""
    return await _run_gitlab_subcommand(
        subcommand,
        runtime,
        output_mode,
        to_file,
        backend=backend if backend is not None else _mock_backend(),
        large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
    )


async def _run_gh(subcommand, runtime, *, to_file=False, backend=None):
    """Invoke the gh tool implementation with a default mock backend + results prefix."""
    return await _run_github_subcommand(
        subcommand,
        runtime,
        to_file,
        backend=backend if backend is not None else _mock_backend(),
        large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
    )


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

            result1 = await _run_gh("issue view 1", runtime)
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

            result2 = await _run_gh("issue view 2", runtime)
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

            result = await _run_gh("issue view 1", runtime)
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

            result = await _run_gh("issue view 1", runtime)
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
            result = await _run_gl(
                f'project-merge-request-discussion create --mr-iid 10 --body "nice" '
                f"--position {json.dumps(position_json)}",
                runtime,
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

            result = await _run_gl(subcommand, runtime)

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

            result = await _run_gl('project-merge-request-discussion create --mr-iid 10 --body "hi"', runtime)

        assert result == "cli-output"
        mock_rc.create_instance.return_value.create_merge_request_inline_discussion.assert_not_called()
        create_proc.assert_called_once()

    async def test_error_when_mr_iid_missing(self):
        runtime = _make_gitlab_runtime()
        position_json = json.dumps(VALID_POSITION)

        with patch("automation.agent.middlewares.git_platform.RepoClient"):
            result = await _run_gl(
                f'project-merge-request-discussion create --body "b" --position {json.dumps(position_json)}', runtime
            )

        assert result.startswith("error:")
        assert "--mr-iid" in result

    async def test_error_when_body_missing(self):
        runtime = _make_gitlab_runtime()
        position_json = json.dumps(VALID_POSITION)

        with patch("automation.agent.middlewares.git_platform.RepoClient"):
            result = await _run_gl(
                f"project-merge-request-discussion create --mr-iid 5 --position {json.dumps(position_json)}", runtime
            )

        assert result.startswith("error:")
        assert "--body" in result

    async def test_error_when_position_is_invalid_json(self):
        runtime = _make_gitlab_runtime()

        with patch("automation.agent.middlewares.git_platform.RepoClient"):
            result = await _run_gl(
                'project-merge-request-discussion create --mr-iid 5 --body "b" --position "not-json"', runtime
            )

        assert result.startswith("error:")
        assert "--position" in result

    async def test_error_when_position_is_not_an_object(self):
        runtime = _make_gitlab_runtime()

        with patch("automation.agent.middlewares.git_platform.RepoClient"):
            result = await _run_gl(
                'project-merge-request-discussion create --mr-iid 5 --body "b" --position "[1,2,3]"', runtime
            )

        assert result.startswith("error:")

    async def test_error_propagated_from_repo_client(self):
        runtime = _make_gitlab_runtime()
        position_json = json.dumps(VALID_POSITION)

        with patch("automation.agent.middlewares.git_platform.RepoClient") as mock_rc:
            mock_rc.create_instance.return_value.create_merge_request_inline_discussion.side_effect = RuntimeError(
                "GitLab 422"
            )

            result = await _run_gl(
                f'project-merge-request-discussion create --mr-iid 10 --body "b" '
                f"--position {json.dumps(position_json)}",
                runtime,
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
            result = await _run_gl(subcommand, runtime)

        assert result.startswith("error:")
        assert "--mr-iid" in result


def test_large_tool_results_prefix_uses_artifacts_root_for_composite():
    backend = DAIVCompositeBackend(default=SandboxFileBackend(), routes={}, artifacts_root="/workspace")
    assert _large_tool_results_prefix(backend) == "/workspace/large_tool_results"


def test_large_tool_results_prefix_defaults_to_root_for_non_composite():
    # A bare backend carries no artifacts_root the middleware would honour, so it falls back to "/".
    assert _large_tool_results_prefix(SandboxFileBackend()) == "/large_tool_results"


def test_file_write_confirmation_shape():
    output = "line1\nline2\nline3"
    msg = _file_write_confirmation("/workspace/large_tool_results/x", 17, 3, output)
    assert "/workspace/large_tool_results/x" in msg
    assert "17 bytes" in msg
    assert "3 lines" in msg
    assert "line1" in msg  # head preview included


def test_file_write_confirmation_caps_preview_to_25_lines():
    output = "\n".join(f"line{i}" for i in range(100))
    msg = _file_write_confirmation("/workspace/large_tool_results/x", 999, 100, output)
    assert "line24" in msg  # 25th line (0-indexed) is shown
    assert "line25" not in msg  # 26th line is not


def test_file_write_confirmation_caps_preview_chars():
    output = "x" * 5000  # single very long line
    msg = _file_write_confirmation("/workspace/large_tool_results/x", 5000, 1, output)
    assert "(preview truncated)" in msg
    assert len(msg) < 5000


async def test_write_output_to_file_writes_via_backend_and_confirms():
    runtime = _make_gitlab_runtime()  # tool_call_id="test_call_gitlab"
    backend = _mock_backend()

    result = await _write_output_to_file(
        "a\nb\nc",
        runtime=runtime,
        backend=backend,
        large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
        tool_name="gitlab",
    )

    path, content = backend.awrite.call_args.args
    # path is keyed by tool_call_id, exactly like the middleware's auto-eviction
    assert path == "/workspace/large_tool_results/test_call_gitlab"
    assert content == "a\nb\nc"  # full content, untruncated
    assert result.startswith("Wrote ")
    assert "3 lines" in result


async def test_write_output_to_file_returns_error_on_backend_failure():
    runtime = _make_gitlab_runtime()
    backend = _mock_backend(error="disk full")

    result = await _write_output_to_file(
        "x", runtime=runtime, backend=backend, large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX, tool_name="gitlab"
    )

    assert result.startswith("error:")
    assert "disk full" in result


async def test_write_output_to_file_returns_error_when_backend_raises():
    """A raised exception from ``awrite`` must be caught and returned as an ``error:`` string,
    not propagated out of the tool (the agent's only channel is the returned string)."""
    runtime = _make_gitlab_runtime()
    backend = _mock_backend()
    backend.awrite = AsyncMock(side_effect=RuntimeError("boom"))

    result = await _write_output_to_file(
        "x", runtime=runtime, backend=backend, large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX, tool_name="gitlab"
    )

    assert result.startswith("error:")
    assert "boom" in result


async def test_write_output_to_file_fails_loudly_when_tool_call_id_missing():
    """Without a tool_call_id the path key would collapse onto a shared filename, silently
    overwriting a prior dump — so the write must be refused with an ``error:`` string instead."""
    runtime = ToolRuntime(
        state={},
        context=Mock(repository=Mock(slug="group/repo"), git_platform=GitPlatform.GITLAB),
        config={},
        stream_writer=Mock(),
        tool_call_id=None,
        store=None,
    )
    backend = _mock_backend()

    result = await _write_output_to_file(
        "x", runtime=runtime, backend=backend, large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX, tool_name="gitlab"
    )

    assert result.startswith("error:")
    assert "tool_call_id" in result
    backend.awrite.assert_not_called()


async def test_gitlab_output_to_file_forces_json_writes_and_confirms():
    runtime = _make_gitlab_runtime()
    backend = _mock_backend()

    mock_settings = Mock()
    mock_settings.GITLAB_AUTH_TOKEN.get_secret_value.return_value = "test-token"  # noqa: S106
    mock_settings.GITLAB_URL.encoded_string.return_value = "https://gitlab.com"

    payload = b'[{"iid": 1}, {"iid": 2}]\n'

    with (
        patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc,
        patch("automation.agent.middlewares.git_platform.settings", mock_settings),
    ):
        proc = Mock()
        proc.communicate = AsyncMock(return_value=(payload, b""))
        proc.returncode = 0
        create_proc.return_value = proc

        result = await _run_gl(
            "project-merge-request list --state opened", runtime, output_mode="detailed", to_file=True, backend=backend
        )

    argv = list(create_proc.call_args.args)
    assert "--output" in argv and argv[argv.index("--output") + 1] == "json"
    assert "--verbose" not in argv  # output_mode ignored when writing to file

    path, content = backend.awrite.call_args.args
    assert path == "/workspace/large_tool_results/test_call_gitlab"
    assert content == '[{"iid": 1}, {"iid": 2}]'  # full, untruncated
    assert result.startswith("Wrote ")
    assert "/workspace/large_tool_results/test_call_gitlab" in result
    assert '"iid": 1' in result  # head preview


async def test_gitlab_output_to_file_does_not_force_json_for_job_trace():
    runtime = _make_gitlab_runtime()
    backend = _mock_backend()
    mock_settings = Mock()
    mock_settings.GITLAB_AUTH_TOKEN.get_secret_value.return_value = "test-token"  # noqa: S106
    mock_settings.GITLAB_URL.encoded_string.return_value = "https://gitlab.com"
    with (
        patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc,
        patch("automation.agent.middlewares.git_platform.settings", mock_settings),
    ):
        proc = Mock()
        proc.communicate = AsyncMock(return_value=(b"log line 1\nlog line 2\n", b""))
        proc.returncode = 0
        create_proc.return_value = proc
        result = await _run_gl("project-job trace --id 55", runtime, to_file=True, backend=backend)
    argv = list(create_proc.call_args.args)
    assert "--output" not in argv  # traces are raw log text; JSON would be degenerate
    assert backend.awrite.call_args.args[0] == "/workspace/large_tool_results/test_call_gitlab"
    assert result.startswith("Wrote ")


async def test_gitlab_empty_output_to_file_notes_no_file_written():
    """When the gitlab CLI returns empty stdout and output_to_file is true, the result must
    contain both the 'empty result' sentinel and a note that no file was written."""
    runtime = _make_gitlab_runtime()
    backend = _mock_backend()

    mock_settings = Mock()
    mock_settings.GITLAB_AUTH_TOKEN.get_secret_value.return_value = "test-token"  # noqa: S106
    mock_settings.GITLAB_URL.encoded_string.return_value = "https://gitlab.com"

    with (
        patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc,
        patch("automation.agent.middlewares.git_platform.settings", mock_settings),
    ):
        proc = Mock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        create_proc.return_value = proc

        result = await _run_gl("project-issue list --state opened", runtime, to_file=True, backend=backend)

    assert "empty result" in result
    assert "no file was written" in result
    backend.awrite.assert_not_called()


@patch("automation.agent.middlewares.git_platform.cache.lock", new=MagicMock())
class TestGitHubToolOutputToFile:
    async def test_github_output_to_file_writes_verbatim_and_wraps_in_command(self):
        runtime = ToolRuntime(
            state={"session_id": "sess-1"},
            context=Mock(repo_id="owner/repo", git_platform=GitPlatform.GITHUB),
            config={"configurable": {"thread_id": "t-gh-of"}},
            stream_writer=Mock(),
            tool_call_id="c1",
            store=None,
        )
        backend = _mock_backend()
        payload = b'{"number": 7}\n'

        with (
            patch("automation.agent.middlewares.git_platform.get_github_integration") as gi,
            patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc,
        ):
            gi.return_value.get_access_token.return_value = Mock(
                token="tok",  # noqa: S106
                expires_at=Mock(timestamp=Mock(return_value=9999999999.0)),
            )
            proc = Mock()
            proc.communicate = AsyncMock(return_value=(payload, b""))
            proc.returncode = 0
            create_proc.return_value = proc

            result = await _run_gh("pr view 7 --json number", runtime, to_file=True, backend=backend)

        # gh is written verbatim — the tool never injects a global --output flag
        argv = list(create_proc.call_args.args)
        assert "--output" not in argv

        path, content = backend.awrite.call_args.args
        assert path == "/workspace/large_tool_results/c1"
        assert content == '{"number": 7}'

        # token was refreshed → Command, and its ToolMessage carries the confirmation
        assert isinstance(result, Command)
        msg = result.update["messages"][0].content
        assert msg.startswith("Wrote ")
        assert "/workspace/large_tool_results/c1" in msg

    async def test_github_empty_output_to_file_notes_no_file_written(self):
        """When gh returns empty stdout and output_to_file is true, the result must contain
        both the 'empty result' sentinel and a note that no file was written."""
        runtime = ToolRuntime(
            state={"session_id": "sess-1", "github_token": "tok", "github_token_expires_at": 9999999999.0},
            context=Mock(repo_id="owner/repo", git_platform=GitPlatform.GITHUB),
            config={"configurable": {"thread_id": "t-gh-empty"}},
            stream_writer=Mock(),
            tool_call_id="c3",
            store=None,
        )
        backend = _mock_backend()
        with patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc:
            proc = Mock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = 0
            create_proc.return_value = proc

            result = await _run_gh("issue list --state open", runtime, to_file=True, backend=backend)

        # Token was already cached → plain string (no Command)
        assert isinstance(result, str)
        assert "empty result" in result
        assert "no file was written" in result
        backend.awrite.assert_not_called()


async def test_tool_descriptions_document_output_to_file():
    for desc in (GITLAB_TOOL_DESCRIPTION, GITHUB_TOOL_DESCRIPTION):
        assert "output_to_file" in desc
        assert "read_file" in desc  # how to consume the saved file
    assert "--output json" in GITLAB_TOOL_DESCRIPTION  # gitlab forces JSON when writing to file
    assert "--json" in GITHUB_TOOL_DESCRIPTION  # gh opts into JSON via its own flag


def test_tool_descriptions_document_project_job_trace_raw_text():
    """GITLAB_TOOL_DESCRIPTION must mention project-job trace together with raw log text."""
    assert "project-job trace" in GITLAB_TOOL_DESCRIPTION
    assert "raw log text" in GITLAB_TOOL_DESCRIPTION


@patch("automation.agent.middlewares.git_platform.cache.lock", new=MagicMock())
class TestGitHubToolOutputToFileExtra:
    async def test_github_run_view_log_to_file(self):
        """gh run view --log to_file: clean_job_logs runs; file written, confirmation returned."""
        runtime = ToolRuntime(
            state={"session_id": "sess-2", "github_token": "tok", "github_token_expires_at": 9999999999.0},
            context=Mock(repo_id="owner/repo", git_platform=GitPlatform.GITHUB),
            config={"configurable": {"thread_id": "t-gh-log"}},
            stream_writer=Mock(),
            tool_call_id="c-log",
            store=None,
        )
        backend = _mock_backend()
        log_output = b"2024-01-01T00:00:00.000Z job1\tsome log line\n"

        with (
            patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc,
            patch(
                "automation.agent.middlewares.git_platform.clean_job_logs", return_value="some log line"
            ) as mock_clean,
        ):
            proc = Mock()
            proc.communicate = AsyncMock(return_value=(log_output, b""))
            proc.returncode = 0
            create_proc.return_value = proc

            result = await _run_gh("run view 123 --job 456 --log", runtime, to_file=True, backend=backend)

        # Token was already cached → plain string (no Command)
        assert isinstance(result, str)
        assert result.startswith("Wrote ")
        assert backend.awrite.call_args.args[0] == "/workspace/large_tool_results/c-log"
        mock_clean.assert_called_once()

    async def test_github_output_to_file_cached_token_plain_string(self):
        """gh output_to_file with a cached valid token returns a plain str (no Command)."""
        runtime = ToolRuntime(
            state={"session_id": "sess-3", "github_token": "tok", "github_token_expires_at": 9999999999.0},
            context=Mock(repo_id="owner/repo", git_platform=GitPlatform.GITHUB),
            config={"configurable": {"thread_id": "t-gh-cached"}},
            stream_writer=Mock(),
            tool_call_id="c-cached",
            store=None,
        )
        backend = _mock_backend()
        payload = b'{"number": 7}\n'

        with patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc:
            proc = Mock()
            proc.communicate = AsyncMock(return_value=(payload, b""))
            proc.returncode = 0
            create_proc.return_value = proc

            result = await _run_gh("pr view 7 --json number", runtime, to_file=True, backend=backend)

        assert isinstance(result, str)
        assert result.startswith("Wrote ")
        assert backend.awrite.call_args.args[0] == "/workspace/large_tool_results/c-cached"


class TestGitPlatformMiddlewareWiring:
    def test_builds_gitlab_tool_and_prefix_from_backend(self):
        backend = DAIVCompositeBackend(default=SandboxFileBackend(), routes={}, artifacts_root="/workspace")
        mw = GitPlatformMiddleware(git_platform=GitPlatform.GITLAB, backend=backend)
        assert mw._large_tool_results_prefix == "/workspace/large_tool_results"
        assert [t.name for t in mw.tools] == ["gitlab"]

    def test_builds_github_tool(self):
        backend = DAIVCompositeBackend(default=SandboxFileBackend(), routes={}, artifacts_root="/workspace")
        mw = GitPlatformMiddleware(git_platform=GitPlatform.GITHUB, backend=backend)
        assert [t.name for t in mw.tools] == ["gh"]

    async def test_gitlab_closure_forwards_backend_and_prefix_end_to_end(self):
        """Invoking the closure-built gitlab tool (not the underscore helper) must write through
        the middleware's own backend, at the prefix derived from that backend's artifacts_root —
        proving the closure captured and forwarded both ``backend`` and ``large_tool_results_prefix``."""
        backend = DAIVCompositeBackend(default=SandboxFileBackend(), routes={}, artifacts_root="/workspace")
        backend.awrite = AsyncMock(return_value=Mock(error=None))
        mw = GitPlatformMiddleware(git_platform=GitPlatform.GITLAB, backend=backend)

        runtime = _make_gitlab_runtime()  # tool_call_id="test_call_gitlab"
        mock_settings = Mock()
        mock_settings.GITLAB_AUTH_TOKEN.get_secret_value.return_value = "test-token"  # noqa: S106
        mock_settings.GITLAB_URL.encoded_string.return_value = "https://gitlab.com"

        with (
            patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc,
            patch("automation.agent.middlewares.git_platform.settings", mock_settings),
        ):
            proc = Mock()
            proc.communicate = AsyncMock(return_value=(b'[{"iid": 1}]\n', b""))
            proc.returncode = 0
            create_proc.return_value = proc

            result = await mw.tools[0].coroutine(
                subcommand="project-merge-request list --state opened", runtime=runtime, output_to_file=True
            )

        path, content = backend.awrite.call_args.args
        assert path == "/workspace/large_tool_results/test_call_gitlab"
        assert content == '[{"iid": 1}]'
        assert result.startswith("Wrote ")


class TestGitHubReleasePolicy:
    @pytest.mark.parametrize(("action", "expected"), [("create", True), ("delete", False)])
    def test_release_actions_follow_policy(self, action, expected):
        allowed, _ = _is_allowed_cli_command("release", action, GITHUB_CLI_ALLOW_COMMANDS)
        assert allowed is expected

    async def test_release_create_reaches_the_cli(self):
        context = Mock(git_platform=GitPlatform.GITHUB)
        context.repository.slug = "owner/repo"
        runtime = ToolRuntime(
            state={"github_token": "tok", "github_token_expires_at": 9999999999.0},
            context=context,
            config={"configurable": {"thread_id": "t-gh-release"}},
            stream_writer=Mock(),
            tool_call_id="c-release",
            store=None,
        )

        with patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc:
            proc = Mock()
            proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
            proc.returncode = 0
            create_proc.return_value = proc

            await _run_gh('release create v1.0.0 --title "v1.0.0" --notes "notes"', runtime)

        assert create_proc.call_args.args[:3] == ("gh", "release", "create")


# ---------------------------------------------------------------------------
# Cross-project access
# ---------------------------------------------------------------------------

from accounts.credentials import CredentialReason, ResolvedCredential  # noqa: E402
from automation.agent.middlewares.git_platform import (  # noqa: E402
    REFUSAL_CREDENTIAL_REJECTED,
    REFUSAL_DISABLED,
    REFUSAL_EXPIRED,
    REFUSAL_INSUFFICIENT_SCOPE,
    REFUSAL_NO_ACTING_USER,
    REFUSAL_NO_CREDENTIAL,
    REFUSAL_PLATFORM_DENIED,
    REFUSAL_PROJECT_IS_A_FLAG,
    REFUSAL_PROJECT_NOT_A_PATH,
    REFUSAL_REVOKED,
    REFUSAL_WRONG_HOST,
    _validate_project,
)

ATTACHED = "group/repo"
OTHER = "other-group/other-repo"


def _xproj_runtime(
    platform: GitPlatform,
    *,
    acting_user_id: int | None = 7,
    acting_platform_uid: str | None = None,
    repo_slug: str = ATTACHED,
):
    context = Mock(
        repository=Mock(slug=repo_slug),
        git_platform=platform,
        acting_user_id=acting_user_id,
        acting_platform_uid=acting_platform_uid,
    )
    return ToolRuntime(
        state={},
        context=context,
        config={"configurable": {"thread_id": "t-xproj"}},
        stream_writer=Mock(),
        tool_call_id="c-xproj",
        store=None,
    )


def _gitlab_settings():
    mock_settings = Mock()
    mock_settings.GITLAB_AUTH_TOKEN.get_secret_value.return_value = "service-token"  # noqa: S106
    mock_settings.GITLAB_URL.encoded_string.return_value = "https://gitlab.com"
    return mock_settings


@contextlib.contextmanager
def _patched_platform(*, resolved=None, returncode=0, stdout=b"ok\n", stderr=b""):
    """Patch the credential service, the audit writer and the subprocess in one place."""
    proc = Mock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    with (
        patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc,
        patch("automation.agent.middlewares.git_platform.settings", _gitlab_settings()),
        patch("automation.agent.middlewares.git_platform.aresolve_access_token") as resolve_mock,
        patch("automation.agent.middlewares.git_platform._record_cross_project_access") as record_mock,
        patch("automation.agent.middlewares.git_platform.ainvalidate_cached_token") as invalidate_mock,
        patch("automation.agent.middlewares.git_platform._acting_person_label", AsyncMock(return_value="Ada")),
    ):
        create_proc.return_value = proc
        resolve_mock.return_value = resolved if resolved is not None else ResolvedCredential(token="person-token")  # noqa: S106
        record_mock.return_value = None
        invalidate_mock.return_value = None
        yield SimpleNamespace(
            create_proc=create_proc, resolve=resolve_mock, record=record_mock, invalidate=invalidate_mock
        )


class TestProjectValidation:
    """T024 — rejected before any credential is read or any subprocess is spawned."""

    def test_empty_takes_the_attached_path(self):
        assert _validate_project("", ATTACHED, GitPlatform.GITLAB) == (None, None)
        assert _validate_project("   ", ATTACHED, GitPlatform.GITLAB) == (None, None)

    def test_equal_to_attached_takes_the_attached_path(self):
        assert _validate_project(ATTACHED, ATTACHED, GitPlatform.GITLAB) == (None, None)

    def test_leading_dash_is_flag_confusion(self):
        assert _validate_project("--project-id=99", ATTACHED, GitPlatform.GITLAB) == (None, REFUSAL_PROJECT_IS_A_FLAG)

    @pytest.mark.parametrize("value", ["group/ repo", "other/re\npo", "group\trepo", "group/repo\x00"])
    def test_whitespace_and_control_characters_are_rejected(self, value):
        assert _validate_project(value, ATTACHED, GitPlatform.GITLAB)[1] == REFUSAL_PROJECT_NOT_A_PATH

    def test_another_project_is_accepted(self):
        assert _validate_project(OTHER, ATTACHED, GitPlatform.GITLAB) == (OTHER, None)

    def test_a_url_on_another_host_is_refused(self):
        _target, refusal = _validate_project("https://elsewhere.example/g/r", ATTACHED, GitPlatform.GITLAB)
        assert refusal == REFUSAL_WRONG_HOST.format(project="https://elsewhere.example/g/r", host="gitlab.com")

    def test_a_url_on_the_configured_host_reduces_to_its_path(self):
        assert _validate_project(f"https://gitlab.com/{OTHER}", ATTACHED, GitPlatform.GITLAB) == (OTHER, None)

    async def test_a_rejected_project_never_spawns_a_subprocess(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gitlab_subcommand(
                "project-issue list",
                runtime,
                "simplified",
                False,
                backend=_mock_backend(),
                large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
                project="--oops",
                cross_project_enabled=True,
            )
        assert result == REFUSAL_PROJECT_IS_A_FLAG
        mocks.create_proc.assert_not_called()
        mocks.resolve.assert_not_called()


class TestIdentitySelection:
    """T025 — the service token for the attached project, the person's for any other."""

    async def test_gitlab_empty_project_uses_the_service_token(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            await _run_gitlab_subcommand(
                "project-issue list",
                runtime,
                "simplified",
                False,
                backend=_mock_backend(),
                large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
                project="",
                cross_project_enabled=True,
            )
        envs = mocks.create_proc.call_args.kwargs["env"]
        assert envs["GITLAB_PRIVATE_TOKEN"] == "service-token"  # noqa: S105
        assert mocks.create_proc.call_args.args[-1] == ATTACHED
        mocks.resolve.assert_not_called()

    async def test_gitlab_self_referential_project_uses_the_service_token(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            await _run_gitlab_subcommand(
                "project-issue list",
                runtime,
                "simplified",
                False,
                backend=_mock_backend(),
                large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
                project=ATTACHED,
                cross_project_enabled=True,
            )
        assert mocks.create_proc.call_args.kwargs["env"]["GITLAB_PRIVATE_TOKEN"] == "service-token"  # noqa: S105
        mocks.resolve.assert_not_called()

    async def test_gitlab_another_project_uses_the_persons_token(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            await _run_gitlab_subcommand(
                "project-issue list",
                runtime,
                "simplified",
                False,
                backend=_mock_backend(),
                large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
                project=OTHER,
                cross_project_enabled=True,
            )
        args = mocks.create_proc.call_args.args
        envs = mocks.create_proc.call_args.kwargs["env"]
        assert envs["GITLAB_PRIVATE_TOKEN"] == "person-token"  # noqa: S105
        assert args[-2:] == ("--project-id", OTHER)
        mocks.record.assert_awaited()

    async def test_github_empty_project_uses_the_installation_token(self):
        runtime = _xproj_runtime(GitPlatform.GITHUB)
        runtime.state["github_token"] = "install-token"  # noqa: S105
        runtime.state["github_token_expires_at"] = 9999999999.0
        with _patched_platform() as mocks:
            await _run_github_subcommand(
                "issue list",
                runtime,
                False,
                backend=_mock_backend(),
                large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
                project="",
                cross_project_enabled=True,
            )
        assert mocks.create_proc.call_args.kwargs["env"]["GH_TOKEN"] == "install-token"  # noqa: S105
        mocks.resolve.assert_not_called()

    async def test_github_another_project_uses_the_persons_token(self):
        runtime = _xproj_runtime(GitPlatform.GITHUB)
        runtime.state["github_token"] = "install-token"  # noqa: S105
        runtime.state["github_token_expires_at"] = 9999999999.0
        with _patched_platform() as mocks:
            result = await _run_github_subcommand(
                "issue list",
                runtime,
                False,
                backend=_mock_backend(),
                large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
                project=OTHER,
                cross_project_enabled=True,
            )
        args = mocks.create_proc.call_args.args
        assert mocks.create_proc.call_args.kwargs["env"]["GH_TOKEN"] == "person-token"  # noqa: S105
        assert args[-2:] == ("--repo", OTHER)
        # T022/T038: the person's token must never reach agent state or a checkpoint.
        assert not isinstance(result, Command)


class TestAttachedProjectIsUnchanged:
    """T026 — FR-003 / FR-019 / SC-005."""

    @pytest.mark.parametrize("enabled", [True, False])
    async def test_gitlab_attached_path_is_identical_either_way(self, enabled):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gitlab_subcommand(
                "project-issue list",
                runtime,
                "simplified",
                False,
                backend=_mock_backend(),
                large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
                project="",
                cross_project_enabled=enabled,
            )
        assert result == "ok"
        assert mocks.create_proc.call_args.args[-2:] == ("--project-id", ATTACHED)
        assert mocks.create_proc.call_args.kwargs["env"]["GITLAB_PRIVATE_TOKEN"] == "service-token"  # noqa: S105
        mocks.record.assert_not_called()

    def test_project_argument_is_absent_from_the_schema_when_off(self):
        backend = DAIVCompositeBackend(default=SandboxFileBackend(), routes={}, artifacts_root="/workspace")
        for platform in (GitPlatform.GITLAB, GitPlatform.GITHUB):
            mw = GitPlatformMiddleware(git_platform=platform, backend=backend)
            assert "project" not in mw.tools[0].args_schema.model_fields

    def test_project_argument_is_present_when_on(self):
        backend = DAIVCompositeBackend(default=SandboxFileBackend(), routes={}, artifacts_root="/workspace")
        for platform in (GitPlatform.GITLAB, GitPlatform.GITHUB):
            mw = GitPlatformMiddleware(git_platform=platform, backend=backend, cross_project_enabled=True)
            assert "project" in mw.tools[0].args_schema.model_fields
            assert mw.tools[0].name in ("gitlab", "gh")

    async def test_a_cross_project_call_is_refused_when_the_capability_is_off(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gitlab_subcommand(
                "project-issue list",
                runtime,
                "simplified",
                False,
                backend=_mock_backend(),
                large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
                project=OTHER,
                cross_project_enabled=False,
            )
        assert result == REFUSAL_DISABLED.format(attached=ATTACHED)
        mocks.create_proc.assert_not_called()


class TestExistingLimitsApplyCrossProject:
    """T064 — FR-018: the allowlist, the blocked GitHub `api` resource and the large-result
    eviction behave identically against another project."""

    async def test_disallowed_gitlab_subcommand_is_refused_identically(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gitlab_subcommand(
                "project-variable delete --key SECRET",
                runtime,
                "simplified",
                False,
                backend=_mock_backend(),
                large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
                project=OTHER,
                cross_project_enabled=True,
            )
        assert result == "error: The subcommand 'project-variable' is not allowed by policy."
        mocks.create_proc.assert_not_called()
        mocks.resolve.assert_not_called()

    async def test_github_api_resource_stays_blocked_cross_project(self):
        runtime = _xproj_runtime(GitPlatform.GITHUB)
        with _patched_platform() as mocks:
            result = await _run_github_subcommand(
                "api /repos/other/other/issues",
                runtime,
                False,
                backend=_mock_backend(),
                large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
                project=OTHER,
                cross_project_enabled=True,
            )
        assert result == "error: The subcommand 'api' is not allowed by policy."
        mocks.create_proc.assert_not_called()

    async def test_oversized_cross_project_result_is_written_to_the_same_dir(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        backend = _mock_backend()
        with _patched_platform(stdout=b'[{"iid": 1}]\n'):
            result = await _run_gitlab_subcommand(
                "project-merge-request list",
                runtime,
                "simplified",
                True,
                backend=backend,
                large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
                project=OTHER,
                cross_project_enabled=True,
            )
        path, _content = backend.awrite.call_args.args
        assert path == f"{LARGE_TOOL_RESULTS_PREFIX}/c-xproj"
        assert result.startswith("Wrote ")


class TestRefusalVocabulary:
    """T035 — one string per cause, each naming the project and the next step."""

    @pytest.mark.parametrize(
        ("reason", "expected"),
        [
            (CredentialReason.NO_ACTING_USER, REFUSAL_NO_ACTING_USER.format(attached=ATTACHED)),
            (CredentialReason.NO_CREDENTIAL, REFUSAL_NO_CREDENTIAL.format(person="Ada", provider="gitlab")),
            (CredentialReason.EXPIRED, REFUSAL_EXPIRED.format(person="Ada", provider="gitlab")),
            (CredentialReason.REVOKED, REFUSAL_REVOKED.format(person="Ada", provider="gitlab")),
            (
                CredentialReason.INSUFFICIENT_SCOPE,
                REFUSAL_INSUFFICIENT_SCOPE.format(person="Ada", provider="gitlab", project=OTHER),
            ),
            (CredentialReason.DISABLED, REFUSAL_DISABLED.format(attached=ATTACHED)),
        ],
    )
    async def test_each_reason_gets_its_own_string(self, reason, expected):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform(resolved=ResolvedCredential(reason=reason)) as mocks:
            result = await _run_gitlab_subcommand(
                "project-issue list",
                runtime,
                "simplified",
                False,
                backend=_mock_backend(),
                large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
                project=OTHER,
                cross_project_enabled=True,
            )
        assert result.startswith("error: ")
        assert result == expected
        mocks.create_proc.assert_not_called()

    async def test_platform_denial_is_ambiguous_between_absent_and_forbidden(self):
        """T029 — the tool must not become an existence oracle for private projects."""
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform(returncode=1, stderr=b"404: 404 Project Not Found"):
            result = await _run_gitlab_subcommand(
                "project-issue list",
                runtime,
                "simplified",
                False,
                backend=_mock_backend(),
                large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
                project=OTHER,
                cross_project_enabled=True,
            )
        assert result == REFUSAL_PLATFORM_DENIED.format(project=OTHER, person="Ada", provider="gitlab")
        assert "may not have access" in result
        assert "may not exist" in result

    async def test_stderr_is_never_echoed_on_the_cross_project_path(self):
        """T030 — stderr can carry token fragments and names the person may not see."""
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        secret = "glpat-SECRETVALUE and secret-group/secret-repo"  # noqa: S105
        with _patched_platform(returncode=1, stderr=secret.encode()):
            result = await _run_gitlab_subcommand(
                "project-issue list",
                runtime,
                "simplified",
                False,
                backend=_mock_backend(),
                large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
                project=OTHER,
                cross_project_enabled=True,
            )
        assert "glpat-SECRETVALUE" not in result
        assert "secret-group/secret-repo" not in result
        assert result.startswith("error: ")

    async def test_attached_project_failures_still_report_stderr(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform(returncode=1, stderr=b"boom"):
            result = await _run_gitlab_subcommand(
                "project-issue list",
                runtime,
                "simplified",
                False,
                backend=_mock_backend(),
                large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
                project="",
                cross_project_enabled=True,
            )
        assert "boom" in result


class TestCrossProjectInlineDiscussionIsRefused:
    """The python-gitlab CLI cannot encode a nested position hash, so inline discussions go
    through RepoClient — which holds the service token, the one identity this path may not use."""

    async def test_inline_discussion_on_another_project_is_refused(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with (
            _patched_platform(),
            patch("automation.agent.middlewares.git_platform._create_gitlab_inline_discussion") as inline_mock,
        ):
            subcommand = (
                'project-merge-request-discussion create --mr-iid 1 --body "x" '
                f"--position '{json.dumps(VALID_POSITION)}'"
            )
            result = await _run_gitlab_subcommand(
                subcommand,
                runtime,
                "simplified",
                False,
                backend=_mock_backend(),
                large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
                project=OTHER,
                cross_project_enabled=True,
            )
        assert result.startswith("error: Inline merge request diff comments")
        inline_mock.assert_not_called()


class TestActingIdentityReachesTheCredentialLookup:
    """T054 — the platform uid, not just the DAIV user, decides whose credential is spent."""

    async def test_the_platform_uid_from_the_run_is_passed_through(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=7, acting_platform_uid="4242")
        with _patched_platform() as mocks:
            await _run_gitlab_subcommand(
                "project-issue list",
                runtime,
                "simplified",
                False,
                backend=_mock_backend(),
                large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
                project=OTHER,
                cross_project_enabled=True,
            )
        assert mocks.resolve.call_args.kwargs["platform_uid"] == "4242"
        assert mocks.resolve.call_args.kwargs["acting_user_id"] == 7

    async def test_a_run_with_no_platform_event_passes_no_uid(self):
        """A signed-in chat run is unambiguous — there is no platform event to cross-check."""
        runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=7)
        with _patched_platform() as mocks:
            await _run_gitlab_subcommand(
                "project-issue list",
                runtime,
                "simplified",
                False,
                backend=_mock_backend(),
                large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
                project=OTHER,
                cross_project_enabled=True,
            )
        assert mocks.resolve.call_args.kwargs["platform_uid"] is None


class TestPlatformFailureClassificationIsAnchored:
    """``401`` was matched as a bare substring against CLI stderr, ahead of the 404/403 list, and
    a match destroyed the person's grant. The CLIs echo the requested object's number, so an
    ordinary not-found on issue 401 read as a dead token."""

    @pytest.mark.parametrize(
        "stderr",
        [
            b"GraphQL: Could not resolve to an Issue with the number of 401. (repository.issue)",
            b"HTTP 404: Not Found (https://api.github.com/repos/o/r/issues/401)",
            b"404 Not Found: run 1401 does not exist",
            b"could not find branch ticket-401",
        ],
        ids=["issue-401", "404-url-with-401", "run-1401", "branch-name"],
    )
    async def test_an_ordinary_not_found_does_not_touch_the_grant(self, stderr):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform(returncode=1, stderr=stderr) as mocks:
            result = await _run_gitlab_subcommand(
                "project-issue list",
                runtime,
                "simplified",
                False,
                backend=_mock_backend(),
                large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
                project=OTHER,
                cross_project_enabled=True,
            )

        mocks.invalidate.assert_not_awaited()
        assert result != REFUSAL_CREDENTIAL_REJECTED.format(person="Ada", provider="gitlab")

    @pytest.mark.parametrize(
        "stderr",
        [b"401 Unauthorized", b"HTTP 401: Bad credentials", b"error: invalid_token", b"401: Unauthorized"],
        ids=["gitlab", "gh", "invalid-token", "gitlab-colon"],
    )
    async def test_a_real_auth_failure_drops_the_cached_token_without_revoking(self, stderr):
        """The stored grant is authoritative only via the refresh endpoint. Dropping the cached
        token makes the next call re-resolve, which refreshes or refuses on the platform's word."""
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform(returncode=1, stderr=stderr) as mocks:
            result = await _run_gitlab_subcommand(
                "project-issue list",
                runtime,
                "simplified",
                False,
                backend=_mock_backend(),
                large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
                project=OTHER,
                cross_project_enabled=True,
            )

        mocks.invalidate.assert_awaited_once()
        assert result == REFUSAL_CREDENTIAL_REJECTED.format(person="Ada", provider="gitlab")
