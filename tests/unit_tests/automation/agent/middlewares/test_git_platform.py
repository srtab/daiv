from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from langchain.tools import ToolRuntime
from langgraph.types import Command
from sessions.models import Session, SessionOrigin

from accounts.credentials import CredentialReason, ResolvedCredential, aresolve_access_token
from automation.agent.middlewares.file_system import DAIVCompositeBackend
from automation.agent.middlewares.git_platform import (
    GITHUB_CLI_ALLOW_COMMANDS,
    GITHUB_TOOL_DESCRIPTION,
    GITLAB_TOOL_DESCRIPTION,
    REFUSAL_BODY_FILE_CROSS_PROJECT,
    REFUSAL_BOT_LABEL_CROSS_PROJECT,
    REFUSAL_CREDENTIAL_REJECTED,
    REFUSAL_DESTRUCTIVE_CROSS_PROJECT,
    REFUSAL_DISABLED,
    REFUSAL_EXPIRED,
    REFUSAL_FILE_ARGUMENT_CROSS_PROJECT,
    REFUSAL_FOREIGN_REFERENCE,
    REFUSAL_FOREIGN_REFERENCE_UNAVAILABLE,
    REFUSAL_INLINE_DISCUSSION_CROSS_PROJECT,
    REFUSAL_INSUFFICIENT_SCOPE,
    REFUSAL_NO_ACTING_USER,
    REFUSAL_NO_CREDENTIAL,
    REFUSAL_PLATFORM_DENIED,
    REFUSAL_PROJECT_IS_A_FLAG,
    REFUSAL_PROJECT_NOT_A_PATH,
    REFUSAL_PROJECT_NOT_OWNER_REPO,
    REFUSAL_QUICK_ACTION_CROSS_PROJECT,
    REFUSAL_REVOKED,
    REFUSAL_UNCHECKED_FLAG_CROSS_PROJECT,
    REFUSAL_UNRECORDED_CROSS_PROJECT,
    REFUSAL_WRONG_HOST,
    GitPlatformMiddleware,
    _decide_target,
    _file_write_confirmation,
    _is_allowed_cli_command,
    _large_tool_results_prefix,
    _names_a_bot_label,
    _record_cross_project_access,
    _repo_relative_flag_values,
    _run_github_subcommand,
    _run_gitlab_subcommand,
    _TargetDecision,
    _validate_project,
    _write_output_to_file,
)
from codebase.base import GitPlatform
from codebase.models import CrossProjectAccessRecord
from core.constants import CROSS_PROJECT_CONTENT_MARKER, CrossProjectOutcome
from tests.unit_tests.conftest import unacquired_backend

LARGE_TOOL_RESULTS_PREFIX = "/workspace/large_tool_results"


def _mock_backend(*, error: str | None = None):
    """Filesystem backend stub whose ``awrite`` records calls and returns a WriteResult-like obj."""
    backend = Mock()
    backend.awrite = AsyncMock(return_value=Mock(error=error))
    return backend


async def _run_gl(
    subcommand,
    runtime,
    *,
    output_mode="simplified",
    to_file=False,
    backend=None,
    project="",
    cross_project_enabled=False,
):
    """Invoke the gitlab tool implementation with a default mock backend + results prefix."""
    return await _run_gitlab_subcommand(
        subcommand,
        runtime,
        output_mode,
        to_file,
        backend=backend if backend is not None else _mock_backend(),
        large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
        project=project,
        cross_project_enabled=cross_project_enabled,
    )


async def _run_gh(subcommand, runtime, *, to_file=False, backend=None, project="", cross_project_enabled=False):
    """Invoke the gh tool implementation with a default mock backend + results prefix."""
    return await _run_github_subcommand(
        subcommand,
        runtime,
        to_file,
        backend=backend if backend is not None else _mock_backend(),
        large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
        project=project,
        cross_project_enabled=cross_project_enabled,
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
    backend = DAIVCompositeBackend(default=unacquired_backend(), routes={}, artifacts_root="/workspace")
    assert _large_tool_results_prefix(backend) == "/workspace/large_tool_results"


def test_large_tool_results_prefix_defaults_to_root_for_non_composite():
    # A bare backend carries no artifacts_root the middleware would honour, so it falls back to "/".
    assert _large_tool_results_prefix(unacquired_backend()) == "/large_tool_results"


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
        backend = DAIVCompositeBackend(default=unacquired_backend(), routes={}, artifacts_root="/workspace")
        mw = GitPlatformMiddleware(git_platform=GitPlatform.GITLAB, backend=backend)
        assert mw._large_tool_results_prefix == "/workspace/large_tool_results"
        assert [t.name for t in mw.tools] == ["gitlab"]

    def test_builds_github_tool(self):
        backend = DAIVCompositeBackend(default=unacquired_backend(), routes={}, artifacts_root="/workspace")
        mw = GitPlatformMiddleware(git_platform=GitPlatform.GITHUB, backend=backend)
        assert [t.name for t in mw.tools] == ["gh"]

    async def test_gitlab_closure_forwards_backend_and_prefix_end_to_end(self):
        """Invoking the closure-built gitlab tool (not the underscore helper) must write through
        the middleware's own backend, at the prefix derived from that backend's artifacts_root —
        proving the closure captured and forwarded both ``backend`` and ``large_tool_results_prefix``."""
        backend = DAIVCompositeBackend(default=unacquired_backend(), routes={}, artifacts_root="/workspace")
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


ATTACHED = "group/repo"
OTHER = "other-group/other-repo"


def _xproj_runtime(
    platform: GitPlatform,
    *,
    acting_user_id: int | None = 7,
    acting_platform_uid: str | None = None,
    acting_user_authenticated: bool = True,
    repo_slug: str = ATTACHED,
):
    context = Mock(
        repository=Mock(slug=repo_slug),
        git_platform=platform,
        acting_user_id=acting_user_id,
        acting_platform_uid=acting_platform_uid,
        acting_user_authenticated=acting_user_authenticated,
    )
    return ToolRuntime(
        state={},
        context=context,
        config={"configurable": {"thread_id": "t-xproj"}},
        stream_writer=Mock(),
        tool_call_id="c-xproj",
        store=None,
    )


@pytest.fixture
def xproj_session(transactional_db):
    """The session of ``_xproj_runtime``'s thread: an allowed fetch restricts it, else its result is withheld."""
    return Session.objects.create(thread_id="t-xproj", origin=SessionOrigin.CHAT, repo_id=ATTACHED)


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
        record_mock.return_value = True
        invalidate_mock.return_value = None
        yield SimpleNamespace(
            create_proc=create_proc, resolve=resolve_mock, record=record_mock, invalidate=invalidate_mock
        )


class TestProjectValidation:
    """Rejected before any credential is read or any subprocess is spawned."""

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
            result = await _run_gl("project-issue list", runtime, project="--oops", cross_project_enabled=True)
        assert result == REFUSAL_PROJECT_IS_A_FLAG
        mocks.create_proc.assert_not_called()
        mocks.resolve.assert_not_called()

    @pytest.mark.parametrize(
        "value",
        [
            "other.host/owner/repo",
            "owner/repo/extra",
            "evil.example:443/repo",
            "git@github.com:owner/repo",
            "owner",
            "owner/..",
            "https://github.com/owner/repo/issues/5",
        ],
    )
    def test_a_github_target_is_owner_and_name_only(self, value):
        """gh reads ``-R HOST/OWNER/REPO``: a third segment would send the person's token to that host."""
        assert _validate_project(value, ATTACHED, GitPlatform.GITHUB) == (None, REFUSAL_PROJECT_NOT_OWNER_REPO)

    @pytest.mark.parametrize(
        ("value", "target"),
        [("owner/repo", "owner/repo"), ("owner/repo.js", "owner/repo.js"), ("https://github.com/o/r", "o/r")],
    )
    def test_a_github_owner_and_name_is_accepted(self, value, target):
        assert _validate_project(value, ATTACHED, GitPlatform.GITHUB) == (target, None)

    @pytest.mark.parametrize("value", ["host:8443/group/repo", "group/../repo", "group//repo", "group/re@po"])
    def test_a_gitlab_path_carries_no_host_or_traversal(self, value):
        assert _validate_project(value, ATTACHED, GitPlatform.GITLAB) == (None, REFUSAL_PROJECT_NOT_A_PATH)

    @pytest.mark.parametrize("value", ["group/sub/repo", "my.group/repo", "12345"])
    def test_a_gitlab_path_may_nest_groups(self, value):
        assert _validate_project(value, ATTACHED, GitPlatform.GITLAB) == (value, None)

    async def test_a_github_host_in_the_project_never_reaches_gh(self):
        runtime = _xproj_runtime(GitPlatform.GITHUB)
        with _patched_platform() as mocks:
            result = await _run_gh("issue list", runtime, project="other.host/owner/repo", cross_project_enabled=True)
        assert result == REFUSAL_PROJECT_NOT_OWNER_REPO
        mocks.create_proc.assert_not_called()
        mocks.resolve.assert_not_called()


class TestIdentitySelection:
    """The service token for the attached project, the person's for any other."""

    async def test_gitlab_empty_project_uses_the_service_token(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            await _run_gl("project-issue list", runtime, project="", cross_project_enabled=True)
        envs = mocks.create_proc.call_args.kwargs["env"]
        assert envs["GITLAB_PRIVATE_TOKEN"] == "service-token"  # noqa: S105
        assert mocks.create_proc.call_args.args[-1] == ATTACHED
        mocks.resolve.assert_not_called()

    async def test_gitlab_self_referential_project_uses_the_service_token(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            await _run_gl("project-issue list", runtime, project=ATTACHED, cross_project_enabled=True)
        assert mocks.create_proc.call_args.kwargs["env"]["GITLAB_PRIVATE_TOKEN"] == "service-token"  # noqa: S105
        mocks.resolve.assert_not_called()

    async def test_gitlab_another_project_uses_the_persons_token(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            await _run_gl("project-issue list", runtime, project=OTHER, cross_project_enabled=True)
        args = mocks.create_proc.call_args.args
        envs = mocks.create_proc.call_args.kwargs["env"]
        # A person's grant is an OAuth token: GitLab resolves PRIVATE-TOKEN against personal
        # access tokens only and 401s anything else, so it has to travel as a bearer.
        assert envs["GITLAB_OAUTH_TOKEN"] == "person-token"  # noqa: S105
        assert "GITLAB_PRIVATE_TOKEN" not in envs
        assert args[-2:] == ("--project-id", OTHER)
        mocks.record.assert_awaited()

    async def test_github_empty_project_uses_the_installation_token(self):
        runtime = _xproj_runtime(GitPlatform.GITHUB)
        runtime.state["github_token"] = "install-token"  # noqa: S105
        runtime.state["github_token_expires_at"] = 9999999999.0
        with _patched_platform() as mocks:
            await _run_gh("issue list", runtime, project="", cross_project_enabled=True)
        assert mocks.create_proc.call_args.kwargs["env"]["GH_TOKEN"] == "install-token"  # noqa: S105
        mocks.resolve.assert_not_called()

    async def test_github_another_project_uses_the_persons_token(self):
        runtime = _xproj_runtime(GitPlatform.GITHUB)
        runtime.state["github_token"] = "install-token"  # noqa: S105
        runtime.state["github_token_expires_at"] = 9999999999.0
        with _patched_platform() as mocks:
            result = await _run_gh("issue list", runtime, project=OTHER, cross_project_enabled=True)
        args = mocks.create_proc.call_args.args
        assert mocks.create_proc.call_args.kwargs["env"]["GH_TOKEN"] == "person-token"  # noqa: S105
        assert args[-2:] == ("--repo", OTHER)
        # The person's token must never reach agent state or a checkpoint.
        assert not isinstance(result, Command)


class TestAttachedProjectIsUnchanged:
    """An empty ``project``, or the capability off, leaves the attached-project path as it was."""

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
        backend = DAIVCompositeBackend(default=unacquired_backend(), routes={}, artifacts_root="/workspace")
        for platform in (GitPlatform.GITLAB, GitPlatform.GITHUB):
            mw = GitPlatformMiddleware(git_platform=platform, backend=backend)
            assert "project" not in mw.tools[0].args_schema.model_fields

    def test_project_argument_is_present_when_on(self):
        backend = DAIVCompositeBackend(default=unacquired_backend(), routes={}, artifacts_root="/workspace")
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
    """The allowlist, the blocked GitHub `api` resource and the large-result eviction behave
    identically against another project."""

    async def test_disallowed_gitlab_subcommand_is_refused_identically(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gl(
                "project-variable delete --key SECRET", runtime, project=OTHER, cross_project_enabled=True
            )
        assert result == "error: The subcommand 'project-variable' is not allowed by policy."
        mocks.create_proc.assert_not_called()
        mocks.resolve.assert_not_called()

    async def test_github_api_resource_stays_blocked_cross_project(self):
        runtime = _xproj_runtime(GitPlatform.GITHUB)
        with _patched_platform() as mocks:
            result = await _run_gh("api /repos/other/other/issues", runtime, project=OTHER, cross_project_enabled=True)
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
    """One string per cause, each naming the project and the next step."""

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
            result = await _run_gl("project-issue list", runtime, project=OTHER, cross_project_enabled=True)
        assert result.startswith("error: ")
        assert result == expected
        mocks.create_proc.assert_not_called()

    async def test_platform_denial_is_ambiguous_between_absent_and_forbidden(self):
        """The tool must not become an existence oracle for private projects."""
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform(returncode=1, stderr=b"404: 404 Project Not Found"):
            result = await _run_gl("project-issue list", runtime, project=OTHER, cross_project_enabled=True)
        assert result == REFUSAL_PLATFORM_DENIED.format(project=OTHER, person="Ada", provider="gitlab")
        assert "may not have access" in result
        assert "may not exist" in result

    async def test_stderr_is_never_echoed_on_the_cross_project_path(self):
        """Stderr can carry token fragments and names the person may not see."""
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        secret = "glpat-SECRETVALUE and secret-group/secret-repo"  # noqa: S105
        with _patched_platform(returncode=1, stderr=secret.encode()):
            result = await _run_gl("project-issue list", runtime, project=OTHER, cross_project_enabled=True)
        assert "glpat-SECRETVALUE" not in result
        assert "secret-group/secret-repo" not in result
        assert result.startswith("error: ")

    async def test_attached_project_failures_still_report_stderr(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform(returncode=1, stderr=b"boom"):
            result = await _run_gl("project-issue list", runtime, project="", cross_project_enabled=True)
        assert "boom" in result


class TestCrossProjectInlineDiscussionIsRefused:
    """The python-gitlab CLI cannot encode a nested position hash, so inline discussions go
    through RepoClient — which holds the service token, the one identity this path may not use."""

    @pytest.mark.parametrize("flag", ["--position", "--pos"])
    async def test_inline_discussion_on_another_project_is_refused_before_any_credential(self, flag):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with (
            _patched_platform() as mocks,
            patch("automation.agent.middlewares.git_platform._create_gitlab_inline_discussion") as inline_mock,
        ):
            subcommand = (
                f"project-merge-request-discussion create --mr-iid 1 --body \"x\" {flag} '{json.dumps(VALID_POSITION)}'"
            )
            result = await _run_gl(subcommand, runtime, project=OTHER, cross_project_enabled=True)
        assert result == REFUSAL_INLINE_DISCUSSION_CROSS_PROJECT.format(attached=ATTACHED, project=OTHER)
        inline_mock.assert_not_called()
        mocks.resolve.assert_not_called()
        mocks.create_proc.assert_not_called()
        assert mocks.record.await_args.kwargs["outcome"] == CrossProjectOutcome.DENIED_POLICY


_GP = "automation.agent.middlewares.git_platform"


def _refusing_resolver(reason=CredentialReason.NO_CREDENTIAL):
    return AsyncMock(return_value=ResolvedCredential(reason=reason))


async def _call_cross_project(runtime, resolve, record=None):
    with (
        patch(f"{_GP}.aresolve_access_token", resolve),
        patch(f"{_GP}._record_cross_project_access", record or AsyncMock()),
        patch(f"{_GP}._acting_person_label", AsyncMock(return_value="Alice")),
    ):
        return await _run_gl("project-issue list", runtime, project=OTHER, cross_project_enabled=True)


async def test_signed_in_run_resolves_by_user_id():
    resolve = _refusing_resolver()
    await _call_cross_project(_xproj_runtime(GitPlatform.GITLAB, acting_user_id=7), resolve)
    assert resolve.await_args.kwargs == {"provider": GitPlatform.GITLAB, "acting_user_id": 7}


async def test_webhook_run_resolves_by_platform_uid_only():
    resolve = _refusing_resolver()
    runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=None, acting_platform_uid="4242")
    await _call_cross_project(runtime, resolve)
    assert resolve.await_args.kwargs == {"provider": GitPlatform.GITLAB, "platform_uid": "4242"}


async def test_a_platform_uid_wins_over_a_user_id():
    record = AsyncMock()
    resolve = _refusing_resolver()
    runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=7, acting_platform_uid="4242")
    await _call_cross_project(runtime, resolve, record)
    assert resolve.await_args.kwargs == {"provider": GitPlatform.GITLAB, "platform_uid": "4242"}
    # The uid's grant was looked up, so the sign-in it was not spent from must not be named.
    assert record.await_args.kwargs["acting_user_id"] is None


async def test_a_policy_refusal_beside_a_platform_uid_names_no_sign_in():
    record = AsyncMock()
    runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=7, acting_platform_uid="4242")
    with patch(f"{_GP}._record_cross_project_access", record):
        await _run_gl("project-branch create --branch x --ref main", runtime, project=OTHER, cross_project_enabled=True)
    assert record.await_args.kwargs["acting_user_id"] is None


async def test_an_unauthenticated_user_id_is_never_spent():
    resolve = _refusing_resolver(CredentialReason.NO_ACTING_USER)
    runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=7, acting_user_authenticated=False)
    result = await _call_cross_project(runtime, resolve)
    assert resolve.await_args.kwargs == {"provider": GitPlatform.GITLAB}
    assert result.startswith("error:")


async def test_an_authenticated_run_without_a_user_is_refused():
    resolve = AsyncMock(wraps=aresolve_access_token)
    runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=None)
    with patch("accounts.credentials._acapability_enabled", AsyncMock(return_value=True)):
        result = await _call_cross_project(runtime, resolve)
    assert resolve.await_args.kwargs == {"provider": GitPlatform.GITLAB}
    assert result == REFUSAL_NO_ACTING_USER.format(attached=ATTACHED)


async def test_webhook_run_refused_when_webhook_runs_disabled():
    record = AsyncMock()
    runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=None, acting_platform_uid="4242")
    result = await _call_cross_project(runtime, _refusing_resolver(CredentialReason.WEBHOOK_RUNS_DISABLED), record)
    assert "issue or merge request event" in result
    assert record.await_args.kwargs["outcome"] == CrossProjectOutcome.DENIED_DISABLED


async def test_the_audit_row_names_the_resolved_owner_on_a_webhook_run():
    record = AsyncMock()
    resolve = AsyncMock(return_value=ResolvedCredential(reason=CredentialReason.REVOKED, user_id=31))
    runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=None, acting_platform_uid="4242")
    await _call_cross_project(runtime, resolve, record)
    assert record.await_args.kwargs["acting_user_id"] == 31


async def test_a_signed_in_refusal_is_attributed_to_the_signed_in_user():
    record = AsyncMock()
    await _call_cross_project(_xproj_runtime(GitPlatform.GITLAB, acting_user_id=7), _refusing_resolver(), record)
    assert record.await_args.kwargs["acting_user_id"] == 7


async def test_an_unauthenticated_user_id_is_never_recorded():
    record = AsyncMock()
    runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=7, acting_user_authenticated=False)
    with patch(f"{_GP}._record_cross_project_access", record):
        await _run_gl("project-branch create --branch x --ref main", runtime, project=OTHER, cross_project_enabled=True)
    assert record.await_args.kwargs["acting_user_id"] is None


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
            result = await _run_gl("project-issue list", runtime, project=OTHER, cross_project_enabled=True)

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
            result = await _run_gl("project-issue list", runtime, project=OTHER, cross_project_enabled=True)

        mocks.invalidate.assert_awaited_once()
        assert result == REFUSAL_CREDENTIAL_REJECTED.format(person="Ada", provider="gitlab")

    async def test_a_failed_cache_drop_still_refuses_and_logs_no_traceback(self, caplog):
        """A traceback carries this frame's locals to Sentry, and stderr can echo the person's token."""
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform(returncode=1, stderr=b"401 Unauthorized glpat-PERSONTOKEN") as mocks:
            mocks.invalidate.side_effect = ConnectionError("cache down")
            with caplog.at_level("DEBUG"):
                result = await _run_gl("project-issue list", runtime, project=OTHER, cross_project_enabled=True)

        assert result == REFUSAL_CREDENTIAL_REJECTED.format(person="Ada", provider="gitlab")
        assert all(record.exc_info is None for record in caplog.records)
        assert "glpat-PERSONTOKEN" not in caplog.text


class TestDestructiveSubcommandsAreRefusedCrossProject:
    """The person genuinely holds the permission, so the platform will not stop these. The action,
    though, can be chosen by issue text somebody else wrote — so refuse the destructive verbs by
    policy outside the attached project, and leave them available on it."""

    @pytest.mark.parametrize(
        "subcommand",
        [
            "project delete-merged-branches",
            "project trigger-pipeline --ref main",
            "project-pipeline cancel --pipeline-id 3",
            "project-pipeline retry --pipeline-id 3",
            "project-pipeline create --ref main",
            "project-branch create --branch x --ref main",
            "project-tag create --tag-name v1 --ref main",
            "project-release create --tag-name v1",
            "project-job retry --job-id 4",
            "project-merge-request-draft-note delete --mr-iid 1 --draft-note-id 2",
        ],
    )
    async def test_gitlab_destructive_verbs_are_refused(self, subcommand):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gl(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        mocks.create_proc.assert_not_called()
        resource, action = subcommand.split()[:2]
        assert result == REFUSAL_DESTRUCTIVE_CROSS_PROJECT.format(
            action=f"{resource} {action}", attached=ATTACHED, project=OTHER
        )

    @pytest.mark.parametrize(
        "subcommand",
        [
            "issue close 1",
            "pr close 2",
            "pr reopen 2",
            "run rerun 5",
            "workflow run ci.yml",
            "cache delete 9",
            "label create bug",
            "label edit bug --color ffffff",
        ],
    )
    async def test_github_destructive_verbs_are_refused(self, subcommand):
        runtime = _xproj_runtime(GitPlatform.GITHUB)
        with _patched_platform() as mocks:
            result = await _run_gh(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        mocks.create_proc.assert_not_called()
        resource, action = subcommand.split()[:2]
        assert result == REFUSAL_DESTRUCTIVE_CROSS_PROJECT.format(
            action=f"{resource} {action}", attached=ATTACHED, project=OTHER
        )

    @pytest.mark.parametrize(
        "subcommand", ["project-pipeline cancel --pipeline-id 3", "project delete-merged-branches"]
    )
    async def test_the_attached_project_keeps_them(self, subcommand):
        """Nothing about the attached project changes: it still runs under DAIV's own identity."""
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            await _run_gl(subcommand, runtime, project="", cross_project_enabled=True)

        mocks.create_proc.assert_called_once()

    async def test_reads_and_comments_still_cross(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            await _run_gl(
                'project-issue-note create --issue-iid 1 --body "hi"',
                runtime,
                project=OTHER,
                cross_project_enabled=True,
            )

        mocks.create_proc.assert_called_once()


def _marked(argv, text):
    return [arg for arg in argv if text in arg and arg.endswith(f"\n\n{CROSS_PROJECT_CONTENT_MARKER}")]


class TestGitLabFlagSpellings:
    """python-gitlab's subcommand parsers accept any unambiguous prefix of an option, so the policy reads
    each flag as the CLI will: ``--state-ev`` is ``--state-event`` and ``--desc`` is ``--description``."""

    @pytest.mark.parametrize(
        ("subcommand", "flag"),
        [
            ("project-merge-request update --iid 1 --state-ev close", "--state-event"),
            ("project-issue update --iid 1 --state close", "--state-event"),
            ("project-issue update --iid 1 --state-ev=close", "--state-event"),
            ("project-merge-request update --iid 1 --target-br main", "--target-branch"),
            ("project-merge-request update --iid 1 --assignee-id 3", "--assignee-id"),
            ("project-issue update --iid 1 --discussion-l true", "--discussion-locked"),
        ],
    )
    async def test_an_abbreviated_denied_flag_is_refused(self, subcommand, flag):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gl(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        resource, action = subcommand.split()[:2]
        assert result == REFUSAL_DESTRUCTIVE_CROSS_PROJECT.format(
            action=f"{resource} {action} {flag}", attached=ATTACHED, project=OTHER
        )
        mocks.create_proc.assert_not_called()
        mocks.resolve.assert_not_called()

    @pytest.mark.parametrize(
        "subcommand", ["project-issue list --state opened", "project-merge-request list --target-branch main"]
    )
    async def test_a_read_filter_sharing_a_denied_name_still_crosses(self, subcommand):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gl(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert result == "ok"
        assert mocks.create_proc.call_args.args[1 : 1 + len(subcommand.split())] == tuple(subcommand.split())

    @pytest.mark.parametrize(
        "subcommand",
        [
            'project-issue create --title t --desc "hello"',
            'project-issue create --title t --desc="hello"',
            'project-issue-note create --issue-iid 1 --bo "hello"',
        ],
    )
    async def test_an_abbreviated_body_flag_is_marked(self, subcommand):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            await _run_gl(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert _marked(mocks.create_proc.call_args.args, "hello")

    async def test_every_repeated_body_flag_is_marked(self):
        """argparse keeps the last value, so marking only the first would publish the second unmarked."""
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            await _run_gl(
                'project-issue create --title t --description "one" --description "two"',
                runtime,
                project=OTHER,
                cross_project_enabled=True,
            )

        argv = mocks.create_proc.call_args.args
        assert _marked(argv, "one")
        assert _marked(argv, "two")

    async def test_an_unknown_or_ambiguous_option_is_refused_before_any_credential(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gl(
                "project-issue update --iid 1 --assignee 3", runtime, project=OTHER, cross_project_enabled=True
            )

        assert result == REFUSAL_UNCHECKED_FLAG_CROSS_PROJECT.format(flag="--assignee", command="project-issue update")
        mocks.resolve.assert_not_called()
        mocks.create_proc.assert_not_called()

    @pytest.mark.parametrize(
        "subcommand",
        [
            "project-issue create --title @/proc/self/environ --description x",
            "project-issue create --title=@/proc/self/environ --description x",
            "project-issue list --search @/etc/hostname",
        ],
    )
    async def test_a_value_read_from_a_file_is_refused(self, subcommand):
        """python-gitlab reads ``@path`` from the worker's disk, where ``/proc/self/environ`` holds the token."""
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gl(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert result == REFUSAL_FILE_ARGUMENT_CROSS_PROJECT
        mocks.resolve.assert_not_called()
        mocks.create_proc.assert_not_called()

    async def test_an_escaped_at_sign_still_crosses(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            await _run_gl(
                'project-issue-note create --issue-iid 1 --body "@@alice please look"',
                runtime,
                project=OTHER,
                cross_project_enabled=True,
            )

        assert _marked(mocks.create_proc.call_args.args, "@@alice please look")


class TestGitHubFlagSpellings:
    """gh's flag parser reads ``-m X``, ``-mX``, ``-m=X``, ``--milestone=X`` and clusters such as ``-db X`` alike."""

    @pytest.mark.parametrize(
        ("subcommand", "flag"),
        [
            ("issue edit 1 -m v1", "--milestone"),
            ("issue edit 1 -mv1", "--milestone"),
            ("issue edit 1 -m=v1", "--milestone"),
            ("issue edit 1 --milestone=v1", "--milestone"),
            ("pr edit 2 -B other", "--base"),
            ("pr edit 2 --base=other", "--base"),
            ("issue create -t T -b x -wm v1", "--milestone"),
            ("issue comment 1 --edit-last", "--edit-last"),
            ("pr comment 2 --edit-last -b x", "--edit-last"),
        ],
    )
    async def test_a_short_or_attached_denied_flag_is_refused(self, subcommand, flag):
        runtime = _xproj_runtime(GitPlatform.GITHUB)
        with _patched_platform() as mocks:
            result = await _run_gh(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        resource, action = subcommand.split()[:2]
        assert result == REFUSAL_DESTRUCTIVE_CROSS_PROJECT.format(
            action=f"{resource} {action} {flag}", attached=ATTACHED, project=OTHER
        )
        mocks.create_proc.assert_not_called()
        mocks.resolve.assert_not_called()

    @pytest.mark.parametrize(
        "subcommand",
        [
            "issue comment 1 -Fnote.md",
            "issue comment 1 -F=note.md",
            "issue comment 1 -F note.md",
            "issue create -t T -T template.md",
            "issue create -t T -wTtemplate.md",
            "issue create -t T --recover state.json",
        ],
    )
    async def test_a_body_the_marker_cannot_reach_is_refused(self, subcommand):
        runtime = _xproj_runtime(GitPlatform.GITHUB)
        with _patched_platform() as mocks:
            result = await _run_gh(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert result == REFUSAL_BODY_FILE_CROSS_PROJECT
        mocks.create_proc.assert_not_called()
        mocks.resolve.assert_not_called()

    @pytest.mark.parametrize(
        "subcommand",
        [
            "issue comment 1 -bhello",
            "issue comment 1 -b=hello",
            "issue comment 1 --body=hello",
            "issue comment 1 -b hello",
            "issue create -t T -wb hello",
            "issue edit 1 -b first --body hello",
        ],
    )
    async def test_every_spelling_of_the_body_is_marked(self, subcommand):
        runtime = _xproj_runtime(GitPlatform.GITHUB)
        with _patched_platform() as mocks:
            await _run_gh(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert _marked(mocks.create_proc.call_args.args, "hello")

    @pytest.mark.parametrize("subcommand", ["issue comment 1 -xbhello", "issue create -t T -wRelsewhere/repo"])
    async def test_a_cluster_with_an_unknown_letter_is_refused(self, subcommand):
        runtime = _xproj_runtime(GitPlatform.GITHUB)
        with _patched_platform() as mocks:
            result = await _run_gh(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert result == REFUSAL_UNCHECKED_FLAG_CROSS_PROJECT.format(
            flag=subcommand.split()[-1], command=" ".join(subcommand.split()[:2])
        )
        mocks.create_proc.assert_not_called()
        mocks.resolve.assert_not_called()

    async def test_a_read_sharing_a_short_body_letter_is_left_alone(self):
        """``run list -b`` is ``--branch``: marking it would break the read."""
        runtime = _xproj_runtime(GitPlatform.GITHUB)
        with _patched_platform() as mocks:
            await _run_gh("run list -b main", runtime, project=OTHER, cross_project_enabled=True)

        assert mocks.create_proc.call_args.args[:5] == ("gh", "run", "list", "-b", "main")


class TestBotLabelsAreNotAddedCrossProject:
    """A body marker cannot cover a label, and a label event starts a DAIV run in the other project as the person."""

    REFUSED_GITLAB = [
        "project-issue update --iid 1 --labels daiv",
        "project-issue update --iid 1 --labels=daiv",
        "project-issue update --iid 1 --label daiv",
        "project-issue update --iid 1 --lab=daiv",
        "project-issue update --iid 1 --labels bug,daiv-max",
        'project-issue update --iid 1 --labels "bug, DAIV-Auto"',
        "project-issue update --iid 1 --labels bug --labels daiv",
        "project-issue create --title t --description x --labels daiv",
        "project-merge-request update --iid 1 --labels daiv-auto",
    ]
    REFUSED_GITHUB = [
        "issue create -t T -b x --label daiv",
        "issue create -t T -b x --label=daiv",
        "issue create -t T -b x -l daiv",
        "issue create -t T -b x -ldaiv",
        "issue create -t T -b x -l=daiv",
        "issue create -t T -b x --label bug,daiv-max",
        "issue create -t T -b x --label bug --label daiv",
        "issue create -t T -b x -l DAIV-Auto",
        "issue create -t T -b x -wl daiv",
        "issue edit 1 --add-label daiv",
        "issue edit 1 --add-label=DAIV",
        "pr edit 2 --add-label bug,daiv-auto",
        "pr edit 2 --add-label bug --add-label daiv",
        "issue edit 1 --add-label '\"daiv\"'",
    ]

    PADDINGS = ["\xa0", "\u3000", "\u2003", "\u2028", "\x0b", "\x0c", "\x1f", "\x85"]
    PADDED_BOT_LABELS = [
        *(f"daiv{pad}" for pad in PADDINGS),
        *(f"{pad}daiv" for pad in PADDINGS),
        *(f"{pad}DAIV-Max{pad}" for pad in PADDINGS),
        *(f'"{pad}daiv-auto{pad}"' for pad in PADDINGS),
        "daiv\u200b",
        "\u200bdaiv",
        "\ufeffdaiv",
        "\uff44\uff41\uff49\uff56",
    ]

    @staticmethod
    def _assert_refused_without_spending(result, mocks):
        assert result == REFUSAL_BOT_LABEL_CROSS_PROJECT.format(attached=ATTACHED, project=OTHER)
        mocks.resolve.assert_not_called()
        mocks.create_proc.assert_not_called()
        assert mocks.record.await_args.kwargs["outcome"] == CrossProjectOutcome.DENIED_POLICY

    @pytest.mark.parametrize("subcommand", REFUSED_GITLAB)
    async def test_gitlab_refuses_a_bot_label_before_any_credential(self, subcommand):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gl(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        self._assert_refused_without_spending(result, mocks)

    @pytest.mark.parametrize("subcommand", REFUSED_GITHUB)
    async def test_github_refuses_a_bot_label_before_any_credential(self, subcommand):
        runtime = _xproj_runtime(GitPlatform.GITHUB)
        with _patched_platform() as mocks:
            result = await _run_gh(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        self._assert_refused_without_spending(result, mocks)

    @pytest.mark.parametrize("label", PADDED_BOT_LABELS, ids=ascii)
    async def test_gitlab_refuses_a_padded_bot_label(self, label):
        """python-gitlab ``strip()``s every label, so ``daiv`` plus any Unicode whitespace is still ``daiv``."""
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gl(
                f"project-issue update --iid 1 --labels 'bug,{label}'",
                runtime,
                project=OTHER,
                cross_project_enabled=True,
            )

        self._assert_refused_without_spending(result, mocks)

    @pytest.mark.parametrize("label", PADDED_BOT_LABELS, ids=ascii)
    @pytest.mark.parametrize("subcommand", ["issue edit 1 --add-label {}", "issue create -t T -b x --label {}"])
    async def test_github_refuses_a_padded_bot_label(self, subcommand, label):
        runtime = _xproj_runtime(GitPlatform.GITHUB)
        with _patched_platform() as mocks:
            result = await _run_gh(subcommand.format(f"'{label}'"), runtime, project=OTHER, cross_project_enabled=True)

        self._assert_refused_without_spending(result, mocks)

    @pytest.mark.parametrize("label", ["bug\xa0", "\u3000daiv-docs", "good first issue", "daiv\xa0docs"], ids=ascii)
    async def test_a_padded_label_that_is_not_a_bot_label_still_crosses(self, label):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gl(
                f"project-issue update --iid 1 --labels 'bug,{label}'",
                runtime,
                project=OTHER,
                cross_project_enabled=True,
            )

        assert result == "ok"
        assert f"bug,{label}" in mocks.create_proc.call_args.args

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("daiv", True),
            ("bug, DAIV-Auto ,x", True),
            ("\x00daiv\x00", True),
            ("'daiv'", True),
            ("bug\uff0cdaiv", True),
            ("daiv-docs", False),
            ("good first issue", False),
            ("", False),
        ],
        ids=ascii,
    )
    def test_names_a_bot_label(self, value, expected):
        assert _names_a_bot_label(value) is expected

    async def test_gitlab_has_no_add_labels_flag_to_slip_past(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gl(
                "project-issue update --iid 1 --add-labels daiv", runtime, project=OTHER, cross_project_enabled=True
            )

        assert result == REFUSAL_UNCHECKED_FLAG_CROSS_PROJECT.format(
            flag="--add-labels", command="project-issue update"
        )
        mocks.resolve.assert_not_called()
        mocks.create_proc.assert_not_called()

    @pytest.mark.parametrize(
        "subcommand",
        [
            "project-issue update --iid 1 --labels bug,daiv-docs",
            "project-issue update --iid 1 --labels bug",
            "project-issue create --title t --description x --labels triage",
            "project-issue list --labels daiv",
            "project-merge-request list --labels daiv-max",
        ],
    )
    async def test_gitlab_other_labels_and_label_filters_still_cross(self, subcommand):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gl(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert result == "ok"
        assert subcommand.split()[-1] in mocks.create_proc.call_args.args

    @pytest.mark.parametrize(
        "subcommand",
        [
            "issue create -t T -b x --label bug,daiv-docs",
            "issue create -t T -b x -l triage",
            "issue create -t T -b x -wl bug",
            "issue edit 1 --add-label bug",
            "issue edit 1 --remove-label daiv",
            "pr edit 2 --remove-label daiv-max",
            "issue list --label daiv",
            "pr list --label daiv-max",
        ],
    )
    async def test_github_other_labels_removals_and_label_filters_still_cross(self, subcommand):
        runtime = _xproj_runtime(GitPlatform.GITHUB)
        with _patched_platform() as mocks:
            result = await _run_gh(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert result == "ok"
        assert mocks.create_proc.call_args.args[:3] == ("gh", *subcommand.split()[:2])

    async def test_the_attached_project_keeps_the_label_on_gitlab(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gl(
                "project-issue update --iid 1 --labels daiv", runtime, project="", cross_project_enabled=True
            )

        assert result == "ok"
        assert mocks.create_proc.call_args.args[:6] == ("gitlab", "project-issue", "update", "--iid", "1", "--labels")
        assert mocks.create_proc.call_args.kwargs["env"]["GITLAB_PRIVATE_TOKEN"] == "service-token"  # noqa: S105
        mocks.record.assert_not_called()

    async def test_the_attached_project_keeps_the_label_on_github(self):
        runtime = _xproj_runtime(GitPlatform.GITHUB)
        with _patched_platform() as mocks:
            decision = await _decide_target(
                "issue",
                "edit",
                ["1", "--add-label", "daiv"],
                "",
                runtime,
                provider=GitPlatform.GITHUB,
                cross_project_enabled=True,
            )

        assert decision == _TargetDecision()
        mocks.resolve.assert_not_called()
        mocks.record.assert_not_called()


class TestGitLabQuickActionsAreRefusedCrossProject:
    """GitLab runs a body line starting with ``/`` as the person, past every denied verb and flag, so the line is
    refused rather than published."""

    @staticmethod
    def _note(body):
        return f'project-issue-note create --issue-iid 1 --body "{body}"'

    @staticmethod
    def _description(body):
        return f'project-issue create --title t --description "{body}"'

    @pytest.mark.parametrize("body", ["/label ~daiv", "/close", "  /merge", "\t/assign @bob", "hello\n/close"])
    @pytest.mark.parametrize("build", [_note, _description], ids=["note", "description"])
    async def test_a_quick_action_line_is_refused_before_any_credential(self, build, body):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gl(build(body), runtime, project=OTHER, cross_project_enabled=True)

        assert result == REFUSAL_QUICK_ACTION_CROSS_PROJECT.format(attached=ATTACHED, project=OTHER)
        mocks.resolve.assert_not_called()
        mocks.create_proc.assert_not_called()
        assert mocks.record.await_args.kwargs["outcome"] == CrossProjectOutcome.DENIED_POLICY

    @pytest.mark.parametrize(
        "subcommand",
        [
            'project-issue create --title t --desc "/close"',
            "project-issue create --title t --desc=/close",
            "project-issue create --title t --description=/close",
            'project-issue-note create --issue-iid 1 --bo "/close"',
            'project-merge-request-note create --mr-iid 1 --body "/merge"',
            'project-issue-discussion create --issue-iid 1 --body "/label ~daiv"',
            'project-issue update --iid 1 --description "/reopen"',
            'project-merge-request-draft-note create --mr-iid 1 --note "/merge"',
        ],
    )
    async def test_every_spelling_of_a_body_flag_is_checked(self, subcommand):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gl(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert result == REFUSAL_QUICK_ACTION_CROSS_PROJECT.format(attached=ATTACHED, project=OTHER)
        mocks.resolve.assert_not_called()
        mocks.create_proc.assert_not_called()

    @pytest.mark.parametrize("body", ["see a/b", "docs live in /etc/hosts, not here", "https://example.com/a/b"])
    @pytest.mark.parametrize("build", [_note, _description], ids=["note", "description"])
    async def test_a_slash_inside_a_line_still_crosses_marked(self, build, body):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gl(build(body), runtime, project=OTHER, cross_project_enabled=True)

        assert result == "ok"
        assert _marked(mocks.create_proc.call_args.args, body)

    async def test_a_slash_in_a_flag_that_is_not_a_body_still_crosses(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gl(
                'project-issue list --search "/close"', runtime, project=OTHER, cross_project_enabled=True
            )

        assert result == "ok"
        mocks.create_proc.assert_called_once()

    async def test_the_attached_project_keeps_quick_actions(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gl(self._note("/close"), runtime, project="", cross_project_enabled=True)

        assert result == "ok"
        assert "/close" in mocks.create_proc.call_args.args
        assert not any(CROSS_PROJECT_CONTENT_MARKER in arg for arg in mocks.create_proc.call_args.args)
        mocks.record.assert_not_called()

    async def test_github_has_no_quick_actions_to_refuse(self):
        runtime = _xproj_runtime(GitPlatform.GITHUB)
        with _patched_platform() as mocks:
            result = await _run_gh(
                'issue comment 1 --body "/close"', runtime, project=OTHER, cross_project_enabled=True
            )

        assert result == "ok"
        assert _marked(mocks.create_proc.call_args.args, "/close")


DENIED = "secret-group/secret-repo"
TARGET_CONTENT = "ACQUISITION-CODENAME-BLUEBIRD"


async def _run_gl_audited(
    runtime,
    *,
    project,
    resolved=None,
    returncode=0,
    stdout=b"ok\n",
    stderr=b"",
    subcommand="project-issue list",
    side_effect=None,
    cross_project_enabled=True,
):
    """``(result, create_proc, resolve)`` with the real audit writer. ``resolved=None`` leaves the
    resolve mock unconfigured, for the tests asserting no credential was spent."""
    proc = Mock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    with (
        patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc,
        patch("automation.agent.middlewares.git_platform.settings", _gitlab_settings()),
        patch("automation.agent.middlewares.git_platform.aresolve_access_token") as resolve,
        patch("automation.agent.middlewares.git_platform.ainvalidate_cached_token", AsyncMock(return_value=None)),
    ):
        create_proc.return_value = proc
        if resolved is not None:
            resolve.return_value = resolved
        if side_effect is not None:
            create_proc.side_effect = side_effect
        result = await _run_gitlab_subcommand(
            subcommand,
            runtime,
            "simplified",
            False,
            backend=_mock_backend(),
            large_tool_results_prefix=LARGE_TOOL_RESULTS_PREFIX,
            project=project,
            cross_project_enabled=cross_project_enabled,
        )
    return result, create_proc, resolve


@pytest.mark.django_db(transaction=True)
class TestNoFallbackToTheServiceIdentity:
    """The natural "fall back so the agent gets an answer" instinct is precisely the leak this forbids."""

    @pytest.mark.parametrize(
        "reason",
        [
            CredentialReason.NO_CREDENTIAL,
            CredentialReason.EXPIRED,
            CredentialReason.REVOKED,
            CredentialReason.INSUFFICIENT_SCOPE,
            CredentialReason.NO_ACTING_USER,
            CredentialReason.DISABLED,
        ],
    )
    async def test_a_credential_denial_spawns_no_process_at_all(self, member_user, reason):
        runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        result, create_proc, _ = await _run_gl_audited(
            runtime, project=DENIED, resolved=ResolvedCredential(reason=reason)
        )

        assert result.startswith("error: ")
        create_proc.assert_not_called()

        records = [r async for r in CrossProjectAccessRecord.objects.filter(target_repo_id=DENIED)]
        assert len(records) == 1
        assert records[0].outcome != CrossProjectAccessRecord.Outcome.ALLOWED
        # The row names the person the run acts for, never DAIV itself, and survives their deletion.
        assert records[0].acting_user_id == member_user.pk
        assert records[0].acting_user_label

    async def test_a_platform_denial_is_not_retried_under_the_service_token(self, member_user):
        runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        result, create_proc, _ = await _run_gl_audited(
            runtime,
            project=DENIED,
            resolved=ResolvedCredential(token="person-token"),  # noqa: S106
            returncode=1,
            stderr=b"404 Project Not Found",
        )

        assert result.startswith("error: ")
        assert create_proc.call_count == 1
        assert create_proc.call_args.kwargs["env"]["GITLAB_OAUTH_TOKEN"] == "person-token"  # noqa: S105
        assert "GITLAB_PRIVATE_TOKEN" not in create_proc.call_args.kwargs["env"]

        outcomes = [r.outcome async for r in CrossProjectAccessRecord.objects.filter(target_repo_id=DENIED)]
        assert outcomes == [CrossProjectAccessRecord.Outcome.DENIED_NO_ACCESS]
        assert (
            await CrossProjectAccessRecord.objects.filter(target_repo_id=DENIED, acting_user_id=member_user.pk).acount()
            == 1
        )


@pytest.mark.django_db(transaction=True)
class TestNoTargetContentSurvivesADenial:
    """Nothing from the refused project may appear in the answer, the audit row, or a state update."""

    async def test_denied_content_is_absent_from_the_result_and_the_record(self, member_user):
        runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        result, _, _ = await _run_gl_audited(
            runtime,
            project=DENIED,
            resolved=ResolvedCredential(token="person-token"),  # noqa: S106
            returncode=1,
            stdout=TARGET_CONTENT.encode(),
            stderr=f"403 Forbidden while reading {TARGET_CONTENT}".encode(),
        )

        assert TARGET_CONTENT not in result
        record = await CrossProjectAccessRecord.objects.aget(target_repo_id=DENIED)
        assert TARGET_CONTENT not in str(record.__dict__)

    async def test_a_denial_returns_a_plain_string_not_a_state_update(self, member_user):
        runtime = _xproj_runtime(GitPlatform.GITHUB, acting_user_id=member_user.pk)
        proc = Mock()
        proc.communicate = AsyncMock(return_value=(TARGET_CONTENT.encode(), b"404 Not Found"))
        proc.returncode = 1
        with (
            patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc,
            patch(
                "automation.agent.middlewares.git_platform.aresolve_access_token",
                return_value=ResolvedCredential(token="person-token"),  # noqa: S106
            ),
            patch("automation.agent.middlewares.git_platform.ainvalidate_cached_token", AsyncMock(return_value=None)),
        ):
            create_proc.return_value = proc
            result = await _run_gh("issue list", runtime, project=DENIED, cross_project_enabled=True)

        assert not isinstance(result, Command)
        assert TARGET_CONTENT not in result
        assert runtime.state == {}


@pytest.mark.django_db(transaction=True)
class TestThePersonsTokenNeverEntersState:
    """Agent state is checkpointed, so a person's token in it is a token at rest under a key nobody revokes."""

    async def test_a_successful_cross_project_github_call_produces_no_command(self, member_user, xproj_session):
        runtime = _xproj_runtime(GitPlatform.GITHUB, acting_user_id=member_user.pk)
        proc = Mock()
        proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
        proc.returncode = 0
        with (
            patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc,
            patch(
                "automation.agent.middlewares.git_platform.aresolve_access_token",
                return_value=ResolvedCredential(token="person-token"),  # noqa: S106
            ),
            patch("automation.agent.middlewares.git_platform._get_cached_github_cli_token") as cached_mock,
        ):
            create_proc.return_value = proc
            result = await _run_gh("issue list", runtime, project="other/repo", cross_project_enabled=True)

        assert result == "ok"
        assert not isinstance(result, Command)
        assert runtime.state == {}
        cached_mock.assert_not_called()

    async def test_an_allowed_cross_project_call_is_recorded_without_the_token(self, member_user, xproj_session):
        runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        result, _, _ = await _run_gl_audited(
            runtime,
            project="other/repo",
            resolved=ResolvedCredential(token="person-token"),  # noqa: S106
        )

        assert result == "ok"
        record = await CrossProjectAccessRecord.objects.aget(target_repo_id="other/repo")
        assert record.outcome == CrossProjectAccessRecord.Outcome.ALLOWED
        assert record.acting_user_id == member_user.pk
        assert record.thread_id == "t-xproj"
        assert "person-token" not in str(record.__dict__)


@pytest.mark.django_db(transaction=True)
class TestNoTokenReachesALogRecord:
    """Exception messages are where secrets usually escape."""

    async def test_a_failed_cross_project_call_logs_no_token(self, member_user, caplog):
        runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        with caplog.at_level("DEBUG"):
            result, _, _ = await _run_gl_audited(
                runtime,
                project=DENIED,
                resolved=ResolvedCredential(token="glpat-PERSONTOKEN"),  # noqa: S106
                returncode=1,
                stderr=b"remote: HTTP Basic: Access denied for glpat-PERSONTOKEN",
            )

        assert "glpat-PERSONTOKEN" not in result
        assert "glpat-PERSONTOKEN" not in caplog.text

    async def test_a_subprocess_failure_logs_no_token(self, member_user, caplog):
        runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        with (
            caplog.at_level("DEBUG"),
            patch(
                "automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec",
                side_effect=OSError("spawn failed"),
            ),
            patch("automation.agent.middlewares.git_platform.settings", _gitlab_settings()),
            patch(
                "automation.agent.middlewares.git_platform.aresolve_access_token",
                return_value=ResolvedCredential(token="glpat-PERSONTOKEN"),  # noqa: S106
            ),
        ):
            result = await _run_gl("project-issue list", runtime, project=DENIED, cross_project_enabled=True)

        assert result.startswith("error: ")
        assert "glpat-PERSONTOKEN" not in result
        assert "glpat-PERSONTOKEN" not in caplog.text


@pytest.mark.django_db(transaction=True)
class TestEveryAttemptLeavesARow:
    """The attempts that spend the token and then fail are the ones most worth a row."""

    async def test_a_timeout_is_recorded(self, member_user):
        runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        result, _, _ = await _run_gl_audited(
            runtime,
            project=DENIED,
            resolved=ResolvedCredential(token="person-token"),  # noqa: S106
            side_effect=TimeoutError,
        )

        assert "timed out" in result
        outcomes = [r.outcome async for r in CrossProjectAccessRecord.objects.filter(target_repo_id=DENIED)]
        assert outcomes == [CrossProjectAccessRecord.Outcome.ERROR]

    async def test_a_failure_to_launch_is_recorded(self, member_user):
        runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        result, _, _ = await _run_gl_audited(
            runtime,
            project=DENIED,
            resolved=ResolvedCredential(token="person-token"),  # noqa: S106
            side_effect=OSError("spawn failed"),
        )

        assert result.startswith("error: ")
        assert "spawn failed" not in result
        outcomes = [r.outcome async for r in CrossProjectAccessRecord.objects.filter(target_repo_id=DENIED)]
        assert outcomes == [CrossProjectAccessRecord.Outcome.ERROR]

    async def test_a_disallowed_subcommand_is_recorded(self, member_user):
        """The allow-list runs before the target decision, so its refusals need their own row."""
        runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        result, create_proc, _ = await _run_gl_audited(
            runtime,
            project=DENIED,
            resolved=ResolvedCredential(token="person-token"),  # noqa: S106
            subcommand="project-hook create --url http://evil.test",
        )

        assert "not allowed by policy" in result
        create_proc.assert_not_called()
        outcomes = [r.outcome async for r in CrossProjectAccessRecord.objects.filter(target_repo_id=DENIED)]
        assert outcomes == [CrossProjectAccessRecord.Outcome.DENIED_POLICY]

    async def test_the_capability_being_off_is_recorded_as_such(self, member_user):
        runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        result, create_proc, _ = await _run_gl_audited(runtime, project=DENIED, cross_project_enabled=False)

        assert "not enabled" in result
        create_proc.assert_not_called()
        outcomes = [r.outcome async for r in CrossProjectAccessRecord.objects.filter(target_repo_id=DENIED)]
        assert outcomes == [CrossProjectAccessRecord.Outcome.DENIED_DISABLED]


@pytest.mark.django_db(transaction=True)
class TestPolicyRefusalsSpendNothing:
    """Resolving a credential before a denied verb would rotate a GitLab refresh token for a call that can never run."""

    @pytest.mark.parametrize(
        ("subcommand", "why"),
        [
            ("project-branch create --branch x --ref main", "denied verb"),
            ("project-issue move --iid 1 --to-project-id 9", "relocates an issue out of the project"),
            ("project-issue update --iid 1 --state-event close", "closes through an allowed verb"),
            ("project-merge-request update --iid 1 --target-branch other", "repoints an MR"),
            ("project-merge-request-note update --mr-iid 1 --note-id 2 --body x", "edits someone else's note"),
            ("project-label create --name x --color '#fff'", "changes project configuration"),
            ("project-issue update --iid 1 --labels daiv", "adds the label that starts a DAIV run there"),
            ('project-issue-note create --issue-iid 1 --body "/close"', "a quick action runs as the person"),
            ("project-merge-request update --iid 1 --state-ev close", "closes through an abbreviated flag"),
            (
                f"project-merge-request-discussion create --mr-iid 1 --position '{json.dumps(VALID_POSITION)}'",
                "an inline diff comment goes through the service token",
            ),
        ],
    )
    async def test_a_policy_refusal_resolves_no_credential(self, member_user, subcommand, why):
        runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        result, create_proc, resolve = await _run_gl_audited(runtime, project=DENIED, subcommand=subcommand)

        assert result.startswith("error: "), why
        create_proc.assert_not_called()
        resolve.assert_not_called()
        outcomes = [r.outcome async for r in CrossProjectAccessRecord.objects.filter(target_repo_id=DENIED)]
        assert outcomes == [CrossProjectAccessRecord.Outcome.DENIED_POLICY]

    async def test_the_same_verbs_still_work_on_the_attached_project(self, member_user):
        runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        result, create_proc, _ = await _run_gl_audited(
            runtime,
            project="",
            resolved=ResolvedCredential(reason=CredentialReason.NO_CREDENTIAL),
            subcommand="project-issue update --iid 1 --state-event close",
        )

        assert not result.startswith("error: ")
        assert create_proc.call_args.kwargs["env"]["GITLAB_PRIVATE_TOKEN"] == "service-token"  # noqa: S105
        assert not await CrossProjectAccessRecord.objects.aexists()


@pytest.mark.django_db(transaction=True)
class TestNoUnattributedCrossProjectWrite:
    """A cross-project write carries a person's attribution, so only the marker tells the webhook it is DAIV's."""

    @pytest.mark.parametrize(
        "subcommand",
        [
            'project-issue-note create --issue-iid 1 --body "hello"',
            'project-issue-note create --body="hello" --issue-iid 1',
            'project-issue create --title t --description "hello"',
            'project-issue create --title t --description="hello"',
        ],
    )
    async def test_a_published_body_carries_the_marker(self, member_user, subcommand):
        runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        _, create_proc, _ = await _run_gl_audited(
            runtime,
            project=DENIED,
            resolved=ResolvedCredential(token="person-token"),  # noqa: S106
            subcommand=subcommand,
        )

        argv = create_proc.call_args.args
        assert any(CROSS_PROJECT_CONTENT_MARKER in arg for arg in argv), argv

    async def test_the_attached_project_body_is_left_alone(self, member_user):
        runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        _, create_proc, _ = await _run_gl_audited(
            runtime,
            project="",
            resolved=ResolvedCredential(reason=CredentialReason.NO_CREDENTIAL),
            subcommand='project-issue-note create --issue-iid 1 --body "hello"',
        )

        argv = create_proc.call_args.args
        assert not any(CROSS_PROJECT_CONTENT_MARKER in arg for arg in argv), argv

    async def test_a_body_read_from_a_file_is_refused_rather_than_published_unmarked(self, member_user):
        runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        result, create_proc, resolve = await _run_gl_audited(
            runtime,
            project=DENIED,
            subcommand="project-issue create --title t --body-file /tmp/note.md",  # noqa: S108
        )

        assert result.startswith("error: ")
        create_proc.assert_not_called()
        resolve.assert_not_called()


@pytest.mark.django_db(transaction=True)
class TestWebhookRunAttribution:
    """A webhook's platform uid decides whose grant is spent, so an unmatched uid is attributed to nobody, even
    when somebody else's grant exists."""

    async def test_an_unmatched_platform_uid_is_attributed_to_nobody(self, member_user):
        from asgiref.sync import sync_to_async

        from accounts.credentials import platform_host
        from accounts.models import PlatformCredential

        def _grant_alice_uid_77():
            credential = PlatformCredential(
                user=member_user,
                provider=GitPlatform.GITLAB.value,
                host=platform_host(GitPlatform.GITLAB),
                platform_uid="77",
                scopes=["api"],
            )
            credential.access_token = "alice-token"  # noqa: S105
            credential.save()

        await sync_to_async(_grant_alice_uid_77)()
        runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=None, acting_platform_uid="999")
        with (
            patch("accounts.credentials.site_settings") as credential_settings,
            patch(f"{_GP}.asyncio.create_subprocess_exec") as create_proc,
        ):
            credential_settings.cross_project_access_enabled = True
            credential_settings.cross_project_webhook_runs_enabled = True
            result = await _run_gl("project-issue list", runtime, project=DENIED, cross_project_enabled=True)

        create_proc.assert_not_called()
        assert result == REFUSAL_NO_CREDENTIAL.format(person="The requesting user", provider="gitlab")
        record = await CrossProjectAccessRecord.objects.aget(target_repo_id=DENIED)
        assert record.acting_user_id is None
        assert record.acting_user_label == "The requesting user"
        assert record.outcome == CrossProjectAccessRecord.Outcome.DENIED_NO_CREDENTIAL


def _installation_runtime() -> ToolRuntime:
    runtime = _xproj_runtime(GitPlatform.GITHUB)
    runtime.state["github_token"] = "install-token"  # noqa: S105
    runtime.state["github_token_expires_at"] = 9999999999.0
    return runtime


class TestGitHubEnterpriseHost:
    """gh reads ``--repo owner/name`` as github.com unless ``GH_HOST`` names another host, and sends ``GH_TOKEN`` only
    to github.com: an enterprise grant has to travel as ``GH_ENTERPRISE_TOKEN`` to its own host."""

    @staticmethod
    async def _cross_project_env(host: str) -> dict[str, str]:
        runtime = _xproj_runtime(GitPlatform.GITHUB)
        with _patched_platform() as mocks, patch(f"{_GP}.platform_host", return_value=host):
            await _run_gh("issue list", runtime, project=OTHER, cross_project_enabled=True)
        return mocks.create_proc.call_args.kwargs["env"]

    async def test_github_com_keeps_gh_token_and_names_the_host(self):
        env = await self._cross_project_env("github.com")

        token_sent = env.get("GH_TOKEN") == "person-token"  # noqa: S105
        assert token_sent
        assert env["GH_HOST"] == "github.com"
        assert "GH_ENTERPRISE_TOKEN" not in env

    async def test_an_enterprise_host_gets_the_enterprise_token_and_no_gh_token(self):
        env = await self._cross_project_env("ghe.example.com")

        token_sent = env.get("GH_ENTERPRISE_TOKEN") == "person-token"  # noqa: S105
        assert token_sent
        assert env["GH_HOST"] == "ghe.example.com"
        assert "GH_TOKEN" not in env

    async def test_the_attached_path_env_is_unchanged(self):
        with _patched_platform() as mocks, patch(f"{_GP}.platform_host", return_value="ghe.example.com"):
            await _run_gh("issue list", _installation_runtime(), project="", cross_project_enabled=True)

        env = mocks.create_proc.call_args.kwargs["env"]
        assert set(env) == {"PATH", "HOME", "GIT_TERMINAL_PROMPT", "NO_COLOR", "GH_TOKEN", "GH_PAGER"}


class TestGitLabConfidentialityStaysPut:
    """Turning ``--confidential`` off on someone else's issue is a disclosure, not an edit."""

    @pytest.mark.parametrize(
        "flag", ["--confidential false", "--confidential=false", "--confid false", "--c=false", "--confidential true"]
    )
    async def test_an_update_naming_it_in_any_spelling_is_refused(self, flag):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gl(
                f"project-issue update --iid 1 {flag}", runtime, project=OTHER, cross_project_enabled=True
            )

        assert result == REFUSAL_DESTRUCTIVE_CROSS_PROJECT.format(
            action="project-issue update --confidential", attached=ATTACHED, project=OTHER
        )
        mocks.resolve.assert_not_called()
        mocks.create_proc.assert_not_called()
        assert mocks.record.await_args.kwargs["outcome"] == CrossProjectOutcome.DENIED_POLICY

    async def test_creating_a_confidential_issue_still_crosses(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gl(
                "project-issue create --title t --description d --confidential true",
                runtime,
                project=OTHER,
                cross_project_enabled=True,
            )

        assert result == "ok"
        assert "--confidential" in mocks.create_proc.call_args.args

    async def test_the_attached_project_keeps_it(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gl(
                "project-issue update --iid 1 --confidential false", runtime, project="", cross_project_enabled=True
            )

        assert result == "ok"
        mocks.resolve.assert_not_called()


class TestGitHubWritesTakeOnlyKnownFlags:
    """gh is installed unpinned, so a flag the gh 2.45 table does not list may write in ways nobody checked."""

    @pytest.mark.parametrize(
        ("subcommand", "flag"),
        [
            ("issue comment 1 --delete-last --yes", "--delete-last"),
            ("pr comment 2 --delete-last", "--delete-last"),
            ("issue edit 1 --remove-milestone", "--remove-milestone"),
            ("issue edit 1 --set-something x", "--set-something"),
            ("issue edit 1 --set-something=x", "--set-something"),
            ("issue create -t T -b x --type bug", "--type"),
            ("pr edit 2 --title T --auto-merge", "--auto-merge"),
        ],
    )
    async def test_an_unknown_long_flag_is_refused_before_any_credential(self, subcommand, flag):
        runtime = _xproj_runtime(GitPlatform.GITHUB)
        with _patched_platform() as mocks:
            result = await _run_gh(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert result == REFUSAL_UNCHECKED_FLAG_CROSS_PROJECT.format(
            flag=flag, command=" ".join(subcommand.split()[:2])
        )
        mocks.resolve.assert_not_called()
        mocks.create_proc.assert_not_called()
        assert mocks.record.await_args.kwargs["outcome"] == CrossProjectOutcome.DENIED_POLICY

    @pytest.mark.parametrize(
        "subcommand",
        [
            "issue comment 1 --body hello",
            "issue comment 1 --body=hello --web",
            "issue edit 1 --title T --add-label bug --add-project Roadmap --body hello",
            "issue create --title T --body hello --assignee me --project Roadmap",
            "pr edit 2 --add-reviewer bob --remove-label wip --body hello",
            "pr comment 2 -b hello --help",
        ],
    )
    async def test_known_flags_still_cross(self, subcommand):
        runtime = _xproj_runtime(GitPlatform.GITHUB)
        with _patched_platform() as mocks:
            result = await _run_gh(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert result == "ok"
        assert _marked(mocks.create_proc.call_args.args, "hello")

    async def test_the_attached_project_keeps_unknown_flags(self):
        with _patched_platform() as mocks:
            result = await _run_gh(
                "issue comment 1 --edit-last --body x", _installation_runtime(), project="", cross_project_enabled=True
            )

        assert result == "ok"
        assert "--edit-last" in mocks.create_proc.call_args.args


class TestCrossProjectCreationAndTimeResets:
    @pytest.mark.parametrize(
        "subcommand",
        [
            "project-issue reset-spent-time --iid 1",
            "project-issue reset-time-estimate --iid 1",
            "project-merge-request reset-spent-time --iid 1",
            "project-merge-request reset-time-estimate --iid 1",
            "project-merge-request create --source-branch f --target-branch main --title t",
        ],
    )
    async def test_gitlab_refuses_them(self, subcommand):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform() as mocks:
            result = await _run_gl(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        resource, action = subcommand.split()[:2]
        assert result == REFUSAL_DESTRUCTIVE_CROSS_PROJECT.format(
            action=f"{resource} {action}", attached=ATTACHED, project=OTHER
        )
        mocks.resolve.assert_not_called()

    @pytest.mark.parametrize("subcommand", ["pr create -t T -b x", "pr create --fill", "pr create -df"])
    async def test_github_refuses_a_pull_request(self, subcommand):
        runtime = _xproj_runtime(GitPlatform.GITHUB)
        with _patched_platform() as mocks:
            result = await _run_gh(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert result == REFUSAL_DESTRUCTIVE_CROSS_PROJECT.format(action="pr create", attached=ATTACHED, project=OTHER)
        mocks.resolve.assert_not_called()

    async def test_the_attached_project_keeps_them(self):
        runtime = _xproj_runtime(GitPlatform.GITLAB)
        with _patched_platform():
            result = await _run_gl(
                "project-issue reset-spent-time --iid 1", runtime, project="", cross_project_enabled=True
            )

        assert result == "ok"


class TestAReferenceToAnotherRepository:
    """gh reads the repository from an issue or pull request URL rather than from ``--repo``."""

    FOREIGN = [
        "issue view https://github.com/acme/secret/issues/1",
        "issue view HTTPS://GITHUB.COM/acme/secret/issues/1",
        "pr review https://github.com/acme/secret/pull/1 --approve",
        "pr view https://github.com/acme/secret/pull/1/files",
        "pr diff --color never https://github.com/acme/secret/pull/1",
        "issue comment https://github.com/acme/secret/issues/1 --body x",
        "issue view -c https://github.com/acme/secret/issues/1",
        "issue view -- https://github.com/acme/secret/issues/1",
        "issue view acme/secret#1",
        "issue view https://ghe.example.com/group/repo/issues/1",
    ]

    @pytest.mark.parametrize("subcommand", FOREIGN)
    async def test_the_attached_path_refuses_it_and_points_at_project(self, subcommand):
        reference = next(arg for arg in subcommand.split() if "/" in arg)
        with _patched_platform() as mocks:
            result = await _run_gh(subcommand, _installation_runtime(), project="", cross_project_enabled=True)

        assert result == REFUSAL_FOREIGN_REFERENCE.format(reference=reference, target=ATTACHED)
        assert "`project`" in result
        mocks.create_proc.assert_not_called()
        mocks.resolve.assert_not_called()

    async def test_with_the_capability_off_it_says_other_repositories_are_unavailable(self):
        subcommand = "issue view https://github.com/acme/secret/issues/1"
        with _patched_platform() as mocks:
            result = await _run_gh(subcommand, _installation_runtime(), project="", cross_project_enabled=False)

        assert result == REFUSAL_FOREIGN_REFERENCE_UNAVAILABLE.format(
            reference="https://github.com/acme/secret/issues/1", target=ATTACHED
        )
        assert "`project`" not in result
        mocks.create_proc.assert_not_called()

    @pytest.mark.parametrize(
        "subcommand",
        [
            "issue view https://github.com/Group/Repo/issues/1",
            "pr view https://github.com/group/repo/pull/2",
            "issue view group/repo#1",
            "issue view 1",
            "issue comment 1 --body https://github.com/acme/secret/issues/1",
            "issue view 1 --json url --jq .url",
            "issue view 1 -t https://github.com/acme/secret/issues/1",
            "search issues https://github.com/acme/secret/issues/1",
        ],
    )
    async def test_the_attached_path_still_runs_its_own_references_and_flag_values(self, subcommand):
        with _patched_platform() as mocks:
            result = await _run_gh(subcommand, _installation_runtime(), project="", cross_project_enabled=True)

        assert result == "ok"
        mocks.create_proc.assert_called_once()

    async def test_the_cross_project_path_refuses_a_third_repository_before_any_credential(self):
        runtime = _xproj_runtime(GitPlatform.GITHUB)
        with _patched_platform() as mocks:
            result = await _run_gh(
                "issue view https://github.com/acme/secret/issues/1", runtime, project=OTHER, cross_project_enabled=True
            )

        assert result == REFUSAL_FOREIGN_REFERENCE.format(
            reference="https://github.com/acme/secret/issues/1", target=OTHER
        )
        mocks.resolve.assert_not_called()
        mocks.create_proc.assert_not_called()
        assert mocks.record.await_args.kwargs == {
            "provider": GitPlatform.GITHUB,
            "target_repo_id": OTHER,
            "outcome": CrossProjectOutcome.DENIED_POLICY,
            "acting_user_id": 7,
        }

    async def test_the_cross_project_path_runs_a_reference_to_its_own_target(self):
        runtime = _xproj_runtime(GitPlatform.GITHUB)
        with _patched_platform() as mocks:
            result = await _run_gh(
                f"issue view https://github.com/{OTHER.upper()}/issues/1",
                runtime,
                project=OTHER,
                cross_project_enabled=True,
            )

        assert result == "ok"
        mocks.resolve.assert_awaited_once()


class TestAnUnrecordedFetchIsWithheld:
    """The ALLOWED row is what keeps a result to the person who fetched it, so a result without one is not returned."""

    @pytest.mark.parametrize(
        ("platform", "run", "subcommand"),
        [(GitPlatform.GITLAB, _run_gl, "project-issue list"), (GitPlatform.GITHUB, _run_gh, "issue list")],
    )
    async def test_the_result_is_withheld(self, platform, run, subcommand):
        runtime = _xproj_runtime(platform)
        with _patched_platform(stdout=TARGET_CONTENT.encode()) as mocks:
            mocks.record.return_value = False
            result = await run(subcommand, runtime, project=OTHER, cross_project_enabled=True)

        assert result == REFUSAL_UNRECORDED_CROSS_PROJECT.format(project=OTHER)
        assert TARGET_CONTENT not in result

    @pytest.mark.django_db(transaction=True)
    async def test_a_fetch_in_a_thread_without_a_session_row_is_withheld(self, member_user):
        runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)

        result, _, _ = await _run_gl_audited(
            runtime,
            project=OTHER,
            resolved=ResolvedCredential(token="person-token"),  # noqa: S106
            stdout=TARGET_CONTENT.encode(),
        )

        assert result == REFUSAL_UNRECORDED_CROSS_PROJECT.format(project=OTHER)
        assert TARGET_CONTENT not in result

    @pytest.mark.django_db(transaction=True)
    async def test_a_failed_record_write_reports_false(self, member_user):
        runtime = _xproj_runtime(GitPlatform.GITLAB, acting_user_id=member_user.pk)
        with patch.object(CrossProjectAccessRecord.objects, "acreate", AsyncMock(side_effect=RuntimeError("db down"))):
            recorded = await _record_cross_project_access(
                runtime,
                provider=GitPlatform.GITLAB,
                target_repo_id=OTHER,
                outcome=CrossProjectOutcome.ALLOWED,
                acting_user_id=member_user.pk,
                person="Ada",
            )

        assert recorded is False


class TestPublishedTextIsRepoRelative:
    def test_flag_values_are_rewritten_in_every_form(self):
        args = [
            "pr",
            "create",
            "--title",
            "Fix /workspace/repo/a.py",
            "--description=See /workspace/repo/b.py",
            "-b",
            "Touches /workspace/repo/c.py",
            "--notes",
            "Released /workspace/repo/f.py",
            "--position",
            '{"new_path": "/workspace/repo/d.py"}',
            "--body-file",
            "/workspace/repo/e.md",
        ]

        assert _repo_relative_flag_values(args) == [
            "pr",
            "create",
            "--title",
            "Fix a.py",
            "--description=See b.py",
            "-b",
            "Touches c.py",
            "--notes",
            "Released f.py",
            "--position",
            '{"new_path": "/workspace/repo/d.py"}',
            "--body-file",
            "/workspace/repo/e.md",
        ]

    @pytest.mark.parametrize("flag", ["-n", "--note", "--message", "--content"])
    def test_other_text_flags_are_rewritten(self, flag):
        assert _repo_relative_flag_values(["x", flag, "See /workspace/repo/a.py"]) == ["x", flag, "See a.py"]

    async def test_a_gitlab_note_body_is_repo_relative(self):
        runtime = _make_gitlab_runtime()
        mock_settings = Mock()
        mock_settings.GITLAB_AUTH_TOKEN.get_secret_value.return_value = "test-token"  # noqa: S106
        mock_settings.GITLAB_URL.encoded_string.return_value = "https://gitlab.com"
        with (
            patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc,
            patch("automation.agent.middlewares.git_platform.settings", mock_settings),
        ):
            proc = Mock()
            proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
            proc.returncode = 0
            create_proc.return_value = proc
            await _run_gl('project-issue-note create --issue-iid 42 --body "See /workspace/repo/daiv/x.py:3"', runtime)

        argv = list(create_proc.call_args.args)
        assert argv[argv.index("--body") + 1] == "See daiv/x.py:3"

    async def test_inline_discussion_body_is_repo_relative(self):
        runtime = _make_gitlab_runtime()
        with patch("automation.agent.middlewares.git_platform.RepoClient") as mock_rc:
            mock_rc.create_instance.return_value.create_merge_request_inline_discussion.return_value = "disc-1"
            position_json = json.dumps(VALID_POSITION)
            await _run_gl(
                f'project-merge-request-discussion create --mr-iid 10 --body "see /workspace/repo/src/foo.py" '
                f"--position {json.dumps(position_json)}",
                runtime,
            )

        mock_rc.create_instance.return_value.create_merge_request_inline_discussion.assert_called_once_with(
            "group/repo", 10, "see src/foo.py", VALID_POSITION
        )

    async def test_a_github_comment_body_is_repo_relative(self):
        runtime = ToolRuntime(
            state={"github_token": "tok", "github_token_expires_at": 9999999999.0},
            context=Mock(repo_id="owner/repo", git_platform=GitPlatform.GITHUB),
            config={"configurable": {"thread_id": "t"}},
            stream_writer=Mock(),
            tool_call_id="c1",
            store=None,
        )
        with patch("automation.agent.middlewares.git_platform.asyncio.create_subprocess_exec") as create_proc:
            proc = Mock()
            proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
            proc.returncode = 0
            create_proc.return_value = proc
            await _run_gh('issue comment 1 -b "See /workspace/repo/daiv/x.py"', runtime)

        argv = list(create_proc.call_args.args)
        assert argv[argv.index("-b") + 1] == "See daiv/x.py"
