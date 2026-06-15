from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from langchain.tools import ToolRuntime
from langgraph.types import Command

from automation.agent.middlewares.file_system import DAIVCompositeBackend, SandboxFileBackend
from automation.agent.middlewares.git_platform import (
    GITHUB_TOOL_DESCRIPTION,
    GITLAB_TOOL_DESCRIPTION,
    GitPlatformMiddleware,
    _file_write_confirmation,
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
