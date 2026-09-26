import io
import json
import tarfile
from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import httpx
import pytest
from git import Repo
from pydantic import SecretStr

from automation.agent.middlewares.file_system import SandboxFileBackend
from automation.agent.middlewares.sandbox import (
    SANDBOX_SYSTEM_PROMPT,
    BashFailure,
    SandboxEgressUnavailableError,
    SandboxMiddleware,
    _run_bash_commands,
)
from core.conf import settings as core_settings
from core.sandbox.schemas import (
    EgressConfigRequest,
    EgressPolicy,
    EgressRule,
    EgressSecret,
    RunCommandResult,
    RunCommandsResponse,
)
from tests.unit_tests.conftest import FakeSandboxClient, sandbox_runtime

if TYPE_CHECKING:
    from pathlib import Path


def _make_agent_runtime(repo_working_dir: str | Path, *, egress: EgressConfigRequest | None = None) -> Mock:
    runtime = Mock()
    runtime.context = Mock()
    runtime.context.gitrepo = Mock(working_dir=str(repo_working_dir))
    runtime.context.sandbox = sandbox_runtime(egress=egress)
    return runtime


def _make_bash_runtime(repo: Repo) -> Mock:
    """Build a ToolRuntime-compatible mock for bash_tool tests."""
    from langchain.tools import ToolRuntime

    runtime = ToolRuntime(
        state={"session_id": "sess_1"},
        context=Mock(gitrepo=repo, sandbox=sandbox_runtime()),
        config={},
        stream_writer=Mock(),
        tool_call_id="call_1",
        store=None,
    )
    return runtime


def _make_middleware(*, close_session: bool = True) -> SandboxMiddleware:
    """Build a SandboxMiddleware with no injected client/backend; tests that exercise the bash
    tool install a client afterwards, and abefore/aafter tests inject their own."""
    return SandboxMiddleware(agent_root="/dummy", close_session=close_session)


def _make_runtime() -> MagicMock:
    """A minimal agent runtime whose ``ctx.sandbox`` carries valid StartSessionRequest inputs."""
    runtime = MagicMock()
    sb = runtime.context.sandbox
    sb.base_image = "img"
    sb.memory_bytes = 1
    sb.cpus = 1
    sb.env_vars = None
    sb.egress = None
    runtime.context.gitrepo.working_dir = "/tmp/repo"  # noqa: S108
    return runtime


def _bash_tool_with_fake_client(client: Mock):
    """Build a fresh SandboxMiddleware with a bound backend over ``client`` and return its bash tool."""
    backend = SandboxFileBackend(client=client)
    backend.bind_session("sess_1")
    middleware = _make_middleware()
    middleware._sandbox_backend = backend
    return middleware.tools[0]


class TestBashTool:
    async def test_bash_tool_returns_commands_json(self):
        """The bash tool surfaces the sandbox's per-command results as ``{"commands": [...]}``.

        The sandbox is authoritative — there is no local checkout to keep in sync — so the
        output carries only ``commands`` (no ``files_changed``)."""
        response = RunCommandsResponse(results=[RunCommandResult(command="echo ok", output="ok", exit_code=0)])

        runtime = _make_bash_runtime(Mock())
        client = Mock()
        client.run_commands = AsyncMock(return_value=response)
        bash_tool = _bash_tool_with_fake_client(client)

        output = await bash_tool.coroutine(command="echo ok", runtime=runtime)

        payload = json.loads(output)
        assert payload == {"commands": [{"command": "echo ok", "output": "ok", "exit_code": 0}]}
        assert "files_changed" not in payload
        client.run_commands.assert_awaited_once()

    async def test_bash_tool_transient_failure_invites_single_retry(self, tmp_path: Path):
        """A transport error (no HTTP response) yields transient guidance: retry once, then stop."""
        repo_dir = tmp_path / "repoX"
        repo_dir.mkdir(parents=True)
        repo = Repo.init(repo_dir)

        runtime = _make_bash_runtime(repo)
        client = Mock()
        client.run_commands = AsyncMock(side_effect=httpx.RequestError("boom"))
        bash_tool = _bash_tool_with_fake_client(client)

        output = await bash_tool.coroutine(command="echo ok", runtime=runtime)

        assert output.startswith("error:")
        assert "retry this exact command once" in output.lower()
        # Must NOT carry the permanent "stop forever" framing — a retry is still warranted.
        assert "unavailable for the rest of this conversation" not in output.lower()

    async def test_bash_tool_permanent_failure_tells_agent_to_stop(self, tmp_path: Path):
        """A non-retryable status (e.g. 403) tells the agent the tool is gone for the run."""
        repo_dir = tmp_path / "repoY"
        repo_dir.mkdir(parents=True)
        repo = Repo.init(repo_dir)

        runtime = _make_bash_runtime(repo)
        err = httpx.HTTPStatusError("forbidden", request=httpx.Request("POST", "x"), response=httpx.Response(403))
        client = Mock()
        client.run_commands = AsyncMock(side_effect=err)
        bash_tool = _bash_tool_with_fake_client(client)

        output = await bash_tool.coroutine(command="echo ok", runtime=runtime)

        assert output.startswith("error:")
        assert "unavailable for the rest of this conversation" in output.lower()
        assert "do not call" in output.lower()

    async def test_bash_tool_raises_when_backend_not_set(self):
        """Calling the bash tool before abefore_agent bound the backend must fail loud."""
        runtime = _make_bash_runtime(Mock())
        middleware = _make_middleware()  # no backend installed
        bash_tool = middleware.tools[0]
        with pytest.raises(RuntimeError, match="bound the sandbox backend"):
            await bash_tool.coroutine(command="echo ok", runtime=runtime)


class TestBashToolPolicyEnforcement:
    """
    Verify that bash_tool enforces the command policy before any sandbox call.
    All tests assert that _run_bash_commands is NOT called when a command is blocked.
    """

    async def _invoke(self, command: str, tmp_path: Path, extra_disallow=(), extra_allow=()):
        """Run bash_tool with a fresh repo and the given global policy settings."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        repo = Repo.init(repo_dir)

        runtime = _make_bash_runtime(repo)
        runtime.tool_call_id = "call_policy"
        runtime.state["session_id"] = "sess_policy"

        client = Mock()
        run_mock = AsyncMock(return_value=RunCommandsResponse(results=[]))
        client.run_commands = run_mock
        bash_tool = _bash_tool_with_fake_client(client)

        with (
            patch.object(core_settings, "SANDBOX_COMMAND_POLICY_DISALLOW", tuple(extra_disallow)),
            patch.object(core_settings, "SANDBOX_COMMAND_POLICY_ALLOW", tuple(extra_allow)),
        ):
            output = await bash_tool.coroutine(command=command, runtime=runtime)
        return output, run_mock

    # --- Default built-in disallow rules ---

    async def test_git_commit_is_blocked(self, tmp_path: Path):
        output, run_mock = await self._invoke("git commit -m 'test'", tmp_path)
        assert output.startswith("error:")
        assert "default_disallow" in output
        run_mock.assert_not_awaited()

    async def test_git_push_is_blocked(self, tmp_path: Path):
        output, run_mock = await self._invoke("git push origin main", tmp_path)
        assert output.startswith("error:")
        assert "default_disallow" in output
        run_mock.assert_not_awaited()

    async def test_git_push_force_is_blocked(self, tmp_path: Path):
        output, run_mock = await self._invoke("git push --force", tmp_path)
        assert output.startswith("error:")
        run_mock.assert_not_awaited()

    async def test_git_reset_hard_is_blocked(self, tmp_path: Path):
        output, run_mock = await self._invoke("git reset --hard HEAD~1", tmp_path)
        assert output.startswith("error:")
        run_mock.assert_not_awaited()

    async def test_git_config_is_blocked(self, tmp_path: Path):
        output, run_mock = await self._invoke("git config --global user.email x@y.com", tmp_path)
        assert output.startswith("error:")
        run_mock.assert_not_awaited()

    async def test_git_rebase_is_blocked(self, tmp_path: Path):
        output, run_mock = await self._invoke("git rebase -i HEAD~3", tmp_path)
        assert output.startswith("error:")
        run_mock.assert_not_awaited()

    async def test_git_clean_is_blocked(self, tmp_path: Path):
        output, run_mock = await self._invoke("git clean -fd", tmp_path)
        assert output.startswith("error:")
        run_mock.assert_not_awaited()

    # --- Safe commands pass through ---

    async def test_pytest_is_allowed(self, tmp_path: Path):
        output, run_mock = await self._invoke("pytest tests/", tmp_path)
        # Policy should not block; the command reaches sandbox.
        run_mock.assert_awaited_once()

    async def test_git_status_is_allowed(self, tmp_path: Path):
        output, run_mock = await self._invoke("git status", tmp_path)
        run_mock.assert_awaited_once()

    async def test_git_diff_is_allowed(self, tmp_path: Path):
        output, run_mock = await self._invoke("git diff HEAD", tmp_path)
        run_mock.assert_awaited_once()

    async def test_git_log_is_allowed(self, tmp_path: Path):
        output, run_mock = await self._invoke("git log --oneline", tmp_path)
        run_mock.assert_awaited_once()

    async def test_make_lint_is_allowed(self, tmp_path: Path):
        output, run_mock = await self._invoke("make lint", tmp_path)
        run_mock.assert_awaited_once()

    # --- Chain bypass attempts ---

    async def test_chained_and_with_git_push_blocks_all(self, tmp_path: Path):
        """pytest && git push must block the entire invocation."""
        output, run_mock = await self._invoke("pytest tests && git push origin main", tmp_path)
        assert output.startswith("error:")
        run_mock.assert_not_awaited()

    async def test_chained_semicolon_with_git_commit_blocks_all(self, tmp_path: Path):
        output, run_mock = await self._invoke("echo safe; git commit -m x", tmp_path)
        assert output.startswith("error:")
        run_mock.assert_not_awaited()

    async def test_chained_pipe_with_git_reset_blocks_all(self, tmp_path: Path):
        output, run_mock = await self._invoke("cat file | git reset --hard", tmp_path)
        assert output.startswith("error:")
        run_mock.assert_not_awaited()

    async def test_chained_or_with_git_push_blocks_all(self, tmp_path: Path):
        output, run_mock = await self._invoke("make build || git push --force", tmp_path)
        assert output.startswith("error:")
        run_mock.assert_not_awaited()

    async def test_git_push_inside_if_body_is_blocked(self, tmp_path: Path):
        output, run_mock = await self._invoke("if true; then git push; fi", tmp_path)
        assert "default_disallow" in output
        run_mock.assert_not_awaited()

    async def test_git_commit_after_assignment_prefix_is_blocked(self, tmp_path: Path):
        output, run_mock = await self._invoke("HUSKY=0 git commit -m wip", tmp_path)
        assert "default_disallow" in output
        run_mock.assert_not_awaited()

    # --- Parse failure → fail-closed ---

    async def test_unmatched_quote_blocks_execution(self, tmp_path: Path):
        output, run_mock = await self._invoke('echo "unclosed', tmp_path)
        assert output.startswith("error:")
        assert "parse" in output.lower()
        run_mock.assert_not_awaited()

    # --- Global policy settings ---

    async def test_global_disallow_blocks_custom_command(self, tmp_path: Path):
        output, run_mock = await self._invoke("danger cmd", tmp_path, extra_disallow=("danger cmd",))
        assert output.startswith("error:")
        assert "global_disallow" in output
        run_mock.assert_not_awaited()

    async def test_a_globally_denied_command_logs_global_disallow(self, tmp_path: Path, caplog):
        with caplog.at_level("WARNING", logger="daiv.tools"):
            _, run_mock = await self._invoke("danger cmd", tmp_path, extra_disallow=("danger cmd",))

        run_mock.assert_not_awaited()
        [record] = [r for r in caplog.records if getattr(r, "event", None) == "bash_policy_denied"]
        assert record.reason_category == "global_disallow"
        assert "reason=global_disallow" in record.getMessage()

    async def test_global_allow_does_not_override_default_disallow(self, tmp_path: Path):
        """Even if SANDBOX_COMMAND_POLICY_ALLOW contains 'git commit', it stays blocked."""
        output, run_mock = await self._invoke("git commit -m x", tmp_path, extra_allow=("git commit",))
        assert output.startswith("error:")
        assert "default_disallow" in output
        run_mock.assert_not_awaited()

    async def test_global_allow_does_not_override_global_disallow(self, tmp_path: Path):
        output, run_mock = await self._invoke(
            "danger cmd --flag", tmp_path, extra_disallow=("danger cmd",), extra_allow=("danger cmd",)
        )
        assert output.startswith("error:")
        assert "global_disallow" in output
        run_mock.assert_not_awaited()

    async def test_global_disallow_matches_case_and_short_flag_order(self, tmp_path: Path):
        output, run_mock = await self._invoke("rm -fr /workspace/tmp/x", tmp_path, extra_disallow=("RM -RF",))
        assert "global_disallow" in output
        run_mock.assert_not_awaited()

    async def test_global_disallow_matches_past_intervening_flags(self, tmp_path: Path):
        output, run_mock = await self._invoke(
            "npm --registry https://r.example publish", tmp_path, extra_disallow=("npm publish",)
        )
        assert "global_disallow" in output
        run_mock.assert_not_awaited()

    async def test_global_disallow_in_a_chain_blocks_all(self, tmp_path: Path):
        output, run_mock = await self._invoke(
            "pytest tests && curl https://example.com", tmp_path, extra_disallow=("curl",)
        )
        assert "global_disallow" in output
        run_mock.assert_not_awaited()

    async def test_a_blank_global_disallow_entry_blocks_nothing(self, tmp_path: Path):
        _, run_mock = await self._invoke("pytest tests/", tmp_path, extra_disallow=("", "   "))
        run_mock.assert_awaited_once()

    # --- Denial message format ---

    async def test_denial_message_contains_reason_category(self, tmp_path: Path):
        output, _ = await self._invoke("git push", tmp_path)
        assert "default_disallow" in output

    async def test_denial_message_contains_matched_rule(self, tmp_path: Path):
        output, _ = await self._invoke("git push", tmp_path)
        assert "git push" in output


class TestRunBashCommands:
    async def test_run_bash_commands_forwards_to_backend(self):
        """_run_bash_commands forwards the command list to the bound backend with fail_fast=True."""
        backend = SandboxFileBackend(client=Mock())
        backend.bind_session("sess_1")
        backend.run_commands = AsyncMock(return_value=RunCommandsResponse(results=[]))

        response = await _run_bash_commands(backend, ["echo ok"])

        assert response is not None
        backend.run_commands.assert_awaited_once()
        assert backend.run_commands.await_args.args[0] == ["echo ok"]
        assert backend.run_commands.await_args.kwargs["fail_fast"] is True

    async def _run_with_error(self, error: Exception) -> object:
        backend = SandboxFileBackend(client=Mock())
        backend.bind_session("sess_1")
        backend.run_commands = AsyncMock(side_effect=error)
        return await _run_bash_commands(backend, ["echo ok"])

    async def test_transport_error_is_transient(self):
        """No HTTP response (timeout/connection blip) → transient: a retry may connect."""
        assert await self._run_with_error(httpx.RequestError("boom")) is BashFailure.TRANSIENT

    @pytest.mark.parametrize("status", [408, 409, 425, 429, 500, 502, 503, 504])
    async def test_retryable_status_is_transient(self, status: int):
        """409 is the per-session lock contention ("Session is busy"): the op never ran, so a retry
        once the lock frees is safe — it must be transient, not permanent."""
        err = httpx.HTTPStatusError("busy", request=httpx.Request("POST", "x"), response=httpx.Response(status))
        assert await self._run_with_error(err) is BashFailure.TRANSIENT

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    async def test_non_retryable_status_is_permanent(self, status: int):
        err = httpx.HTTPStatusError("nope", request=httpx.Request("POST", "x"), response=httpx.Response(status))
        assert await self._run_with_error(err) is BashFailure.PERMANENT


class TestSandboxMiddleware:
    async def test_abefore_agent_first_turn_creates_and_binds_with_injected_client(self):
        client = MagicMock()
        client.start_session = AsyncMock(return_value="sess-1")
        client.seed_session = AsyncMock()
        sandbox_backend = SandboxFileBackend(client=client)
        mw = SandboxMiddleware(agent_root="/workspace/repo", client=client, sandbox_backend=sandbox_backend)

        with (
            patch("automation.agent.middlewares.sandbox._make_repo_archive", return_value=b""),
            patch("automation.agent.middlewares.sandbox._make_global_skills_archive", return_value=None),
        ):
            result = await mw.abefore_agent({}, _make_runtime())  # empty state => first turn

        assert result == {"session_id": "sess-1"}
        assert sandbox_backend._session_id == "sess-1"  # session bound onto the injected backend

    async def test_abefore_agent_reuses_live_session_from_state_without_reseeding(self):
        client = MagicMock()
        client.session_exists = AsyncMock(return_value=True)
        client.start_session = AsyncMock()
        sandbox_backend = SandboxFileBackend(client=client)
        mw = SandboxMiddleware(agent_root="/workspace/repo", client=client, sandbox_backend=sandbox_backend)

        result = await mw.abefore_agent({"session_id": "sess-prev"}, _make_runtime())

        assert result == {"session_id": "sess-prev"}
        client.start_session.assert_not_awaited()  # warm reuse — no new session, no re-seed
        assert sandbox_backend._session_id == "sess-prev"

    async def test_abefore_agent_recreates_when_state_session_is_dead(self):
        client = MagicMock()
        client.session_exists = AsyncMock(return_value=False)
        client.start_session = AsyncMock(return_value="sess-new")
        client.seed_session = AsyncMock()
        mw = SandboxMiddleware(
            agent_root="/workspace/repo", client=client, sandbox_backend=SandboxFileBackend(client=client)
        )

        with (
            patch("automation.agent.middlewares.sandbox._make_repo_archive", return_value=b""),
            patch("automation.agent.middlewares.sandbox._make_global_skills_archive", return_value=None),
        ):
            result = await mw.abefore_agent({"session_id": "sess-stale"}, _make_runtime())

        assert result == {"session_id": "sess-new"}
        client.start_session.assert_awaited_once()

    async def test_abefore_agent_closes_session_on_seed_failure(self):
        """If seed_session raises, the started session is force-removed; the injected client is NOT closed."""
        client = MagicMock()
        client.start_session = AsyncMock(return_value="sess-leaky")
        client.seed_session = AsyncMock(side_effect=RuntimeError("simulated seed failure"))
        client.close_session = AsyncMock()
        client.close = AsyncMock()
        mw = SandboxMiddleware(
            agent_root="/workspace/repo", client=client, sandbox_backend=SandboxFileBackend(client=client)
        )

        with (
            patch("automation.agent.middlewares.sandbox._make_repo_archive", return_value=b""),
            patch("automation.agent.middlewares.sandbox._make_global_skills_archive", return_value=None),
            pytest.raises(RuntimeError, match="simulated seed failure"),
        ):
            await mw.abefore_agent({}, _make_runtime())

        client.close_session.assert_awaited_once_with("sess-leaky", force=True)
        client.close.assert_not_awaited()  # the run owns the transport, not the middleware

    async def test_subagent_path_returns_without_binding(self):
        client = MagicMock()
        mw = SandboxMiddleware(agent_root="/x", client=client, sandbox_backend=None, close_session=False)
        assert await mw.abefore_agent({"session_id": "sess-1"}, _make_runtime()) is None

    async def test_aafter_agent_resumable_keeps_session_warm_and_in_state(self):
        client = MagicMock()
        client.close = AsyncMock()
        client.close_session = AsyncMock()
        mw = SandboxMiddleware(
            agent_root="/workspace/repo", client=client, sandbox_backend=SandboxFileBackend(client=client)
        )
        with patch("automation.agent.middlewares.sandbox.conversation_thread_id", return_value="thread-1"):
            result = await mw.aafter_agent({"session_id": "sess-1"}, _make_runtime())
        client.close_session.assert_awaited_once_with("sess-1", force=False)  # stop, keep warm
        client.close.assert_not_awaited()  # injected transport is the run's, not the middleware's
        assert result is None  # session_id NOT nulled => next turn reuses it from state

    async def test_aafter_agent_one_shot_force_removes_and_clears_state(self):
        client = MagicMock()
        client.close = AsyncMock()
        client.close_session = AsyncMock()
        mw = SandboxMiddleware(
            agent_root="/workspace/repo", client=client, sandbox_backend=SandboxFileBackend(client=client)
        )
        with patch("automation.agent.middlewares.sandbox.conversation_thread_id", return_value=None):
            result = await mw.aafter_agent({"session_id": "sess-1"}, _make_runtime())
        client.close_session.assert_awaited_once_with("sess-1", force=True)
        client.close.assert_not_awaited()
        assert result == {"session_id": None}

    async def test_aafter_agent_swallows_already_closed_session(self):
        """A 404 from close_session (warm session reaped) is swallowed; resumable run still keeps state."""
        err = httpx.HTTPStatusError("gone", request=httpx.Request("DELETE", "x"), response=httpx.Response(404))
        client = MagicMock()
        client.close = AsyncMock()
        client.close_session = AsyncMock(side_effect=err)
        mw = SandboxMiddleware(
            agent_root="/workspace/repo", client=client, sandbox_backend=SandboxFileBackend(client=client)
        )
        with patch("automation.agent.middlewares.sandbox.conversation_thread_id", return_value="thread-1"):
            result = await mw.aafter_agent({"session_id": "warm-1"}, _make_runtime())
        assert result is None
        client.close.assert_not_awaited()

    async def test_aafter_agent_subagent_does_not_close_session(self):
        client = MagicMock()
        client.close = AsyncMock()
        client.close_session = AsyncMock()
        mw = SandboxMiddleware(agent_root="/x", client=client, sandbox_backend=None, close_session=False)
        result = await mw.aafter_agent({"session_id": "sess-1"}, _make_runtime())
        assert result is None
        client.close_session.assert_not_awaited()
        client.close.assert_not_awaited()

    async def test_abefore_agent_refreshes_egress_on_warm_reuse(self):
        """A reused warm session gets this run's fresh egress pushed onto it before binding."""
        client = MagicMock()
        client.session_exists = AsyncMock(return_value=True)
        client.update_egress = AsyncMock()
        client.start_session = AsyncMock()
        sandbox_backend = SandboxFileBackend(client=client)
        mw = SandboxMiddleware(agent_root="/workspace/repo", client=client, sandbox_backend=sandbox_backend)

        runtime = _make_runtime()
        runtime.context.sandbox.egress = MagicMock()  # run has an egress config (network-on)

        result = await mw.abefore_agent({"session_id": "sess-prev"}, runtime)

        assert result == {"session_id": "sess-prev"}
        client.update_egress.assert_awaited_once_with("sess-prev", runtime.context.sandbox.egress)
        client.start_session.assert_not_awaited()  # warm reuse — no recreate
        assert sandbox_backend._session_id == "sess-prev"

    @pytest.mark.parametrize("refresh_error", ["http_status", "transport"])
    async def test_abefore_agent_recreates_when_egress_refresh_fails(self, refresh_error):
        """A failed egress refresh — an HTTP status error (e.g. 404 on an old sandbox) or a transport
        error (httpx.RequestError) — closes the stale session and recreates it instead of reusing it."""
        error = (
            httpx.HTTPStatusError("nope", request=httpx.Request("PUT", "x"), response=httpx.Response(404))
            if refresh_error == "http_status"
            else httpx.ConnectError("refused")
        )
        client = MagicMock()
        client.session_exists = AsyncMock(return_value=True)
        client.update_egress = AsyncMock(side_effect=error)
        client.start_session = AsyncMock(return_value="sess-new")
        client.seed_session = AsyncMock()
        client.close_session = AsyncMock()
        mw = SandboxMiddleware(
            agent_root="/workspace/repo", client=client, sandbox_backend=SandboxFileBackend(client=client)
        )

        runtime = _make_runtime()
        runtime.context.sandbox.egress = EgressConfigRequest()  # non-None so refresh is attempted

        with (
            patch("automation.agent.middlewares.sandbox._make_repo_archive", return_value=b""),
            patch("automation.agent.middlewares.sandbox._make_global_skills_archive", return_value=None),
        ):
            result = await mw.abefore_agent({"session_id": "sess-stale"}, runtime)

        assert result == {"session_id": "sess-new"}
        client.start_session.assert_awaited_once()  # recreated
        client.close_session.assert_awaited_once_with("sess-stale", force=True)

    async def test_abefore_agent_skips_refresh_when_no_egress(self):
        """A token-less / network-off run (egress is None) reuses the warm session without refreshing."""
        client = MagicMock()
        client.session_exists = AsyncMock(return_value=True)
        client.update_egress = AsyncMock()
        client.start_session = AsyncMock()
        sandbox_backend = SandboxFileBackend(client=client)
        mw = SandboxMiddleware(agent_root="/workspace/repo", client=client, sandbox_backend=sandbox_backend)

        # _make_runtime() sets sandbox.egress = None by default.
        result = await mw.abefore_agent({"session_id": "sess-prev"}, _make_runtime())

        assert result == {"session_id": "sess-prev"}
        client.update_egress.assert_not_awaited()
        client.start_session.assert_not_awaited()

    async def test_abefore_agent_passes_skills_archive_when_skills_dir_populated(self, tmp_path: Path):
        repo_dir = tmp_path / "repoX"
        repo_dir.mkdir(parents=True)
        (repo_dir / "README.md").write_text("hello")

        builtin = tmp_path / "builtin"
        (builtin / "skill-one").mkdir(parents=True)
        (builtin / "skill-one" / "SKILL.md").write_text("hi")

        runtime = _make_agent_runtime(repo_working_dir=str(repo_dir))

        client = MagicMock()
        client.start_session = AsyncMock(return_value="sess_skills")
        client.seed_session = AsyncMock()

        with (
            patch("automation.agent.middlewares.sandbox.BUILTIN_SKILLS_PATH", builtin),
            patch("automation.agent.middlewares.sandbox.agent_settings") as settings,
        ):
            settings.CUSTOM_SKILLS_PATH = None
            middleware = SandboxMiddleware(
                agent_root=f"/{repo_dir.name}", client=client, sandbox_backend=SandboxFileBackend(client=client)
            )
            update = await middleware.abefore_agent({}, runtime)

        assert update == {"session_id": "sess_skills"}
        client.seed_session.assert_awaited_once()
        _args, kwargs = client.seed_session.call_args
        assert isinstance(kwargs.get("repo_archive"), (bytes, bytearray))
        assert isinstance(kwargs.get("skills_archive"), (bytes, bytearray))

    def test_make_global_skills_archive_packs_builtin_and_custom(self, tmp_path: Path):
        from automation.agent.middlewares.sandbox import _make_global_skills_archive

        builtin = tmp_path / "builtin"
        custom = tmp_path / "custom"
        (builtin / "code-review").mkdir(parents=True)
        (builtin / "code-review" / "SKILL.md").write_text("hi")
        (custom / "deploy").mkdir(parents=True)
        (custom / "deploy" / "SKILL.md").write_text("yo")

        with (
            patch("automation.agent.middlewares.sandbox.BUILTIN_SKILLS_PATH", builtin),
            patch("automation.agent.middlewares.sandbox.agent_settings") as settings,
        ):
            settings.CUSTOM_SKILLS_PATH = custom
            archive = _make_global_skills_archive()

        assert isinstance(archive, (bytes, bytearray))
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
            names = set(tf.getnames())
        assert "code-review/SKILL.md" in names
        assert "deploy/SKILL.md" in names

    def test_make_global_skills_archive_none_when_no_skills(self, tmp_path: Path):
        from automation.agent.middlewares.sandbox import _make_global_skills_archive

        empty = tmp_path / "builtin-empty"
        empty.mkdir()
        with (
            patch("automation.agent.middlewares.sandbox.BUILTIN_SKILLS_PATH", empty),
            patch("automation.agent.middlewares.sandbox.agent_settings") as settings,
        ):
            settings.CUSTOM_SKILLS_PATH = None
            assert _make_global_skills_archive() is None

    def test_make_global_skills_archive_skips_unreadable_root_and_still_packs_builtins(self, tmp_path: Path):
        """An OSError reading one root (e.g. a bad-perms custom dir) must not abort the whole
        archive — builtins still seed. (Multi-root behavior change vs the old single-root helper.)"""
        from automation.agent.middlewares.sandbox import _make_global_skills_archive

        builtin = tmp_path / "builtin"
        (builtin / "code-review").mkdir(parents=True)
        (builtin / "code-review" / "SKILL.md").write_text("hi")

        bad_custom = Mock()
        bad_custom.is_dir.return_value = True
        bad_custom.iterdir.side_effect = PermissionError("denied")

        with (
            patch("automation.agent.middlewares.sandbox.BUILTIN_SKILLS_PATH", builtin),
            patch("automation.agent.middlewares.sandbox.agent_settings") as settings,
        ):
            settings.CUSTOM_SKILLS_PATH = bad_custom
            archive = _make_global_skills_archive()

        assert isinstance(archive, (bytes, bytearray))
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
            names = set(tf.getnames())
        assert "code-review/SKILL.md" in names

    def test_make_global_skills_archive_returns_none_on_tar_error(self, tmp_path: Path):
        """A TarError mid-build must not abort the whole sandbox seed — returns None (seed without skills)."""
        from automation.agent.middlewares.sandbox import _make_global_skills_archive

        builtin = tmp_path / "builtin"
        (builtin / "code-review").mkdir(parents=True)
        (builtin / "code-review" / "SKILL.md").write_text("hi")

        with (
            patch("automation.agent.middlewares.sandbox.BUILTIN_SKILLS_PATH", builtin),
            patch("automation.agent.middlewares.sandbox.agent_settings") as settings,
            patch("automation.agent.middlewares.sandbox.tarfile.open", side_effect=tarfile.TarError("boom")),
        ):
            settings.CUSTOM_SKILLS_PATH = None
            assert _make_global_skills_archive() is None

    async def test_awrap_model_call_appends_sandbox_system_prompt(self, tmp_path: Path):
        from langchain.agents.middleware import ModelRequest, ModelResponse

        runtime = _make_agent_runtime(repo_working_dir=str(tmp_path / "repoX"))

        middleware = _make_middleware()

        seen_prompt: str | None = None

        async def handler(request: ModelRequest) -> ModelResponse:
            nonlocal seen_prompt
            seen_prompt = request.system_prompt
            return ModelResponse(result=[])

        request = ModelRequest(model=Mock(), messages=[], system_prompt="base prompt", state=Mock(), runtime=runtime)

        _ = await middleware.awrap_model_call(request, handler)
        assert seen_prompt is not None
        assert seen_prompt.startswith("base prompt")
        assert SANDBOX_SYSTEM_PROMPT in seen_prompt

    async def test_abefore_agent_builds_start_session_from_ctx_sandbox(self, tmp_path: Path):
        """abefore_agent must build StartSessionRequest from ``ctx.sandbox``, not ``ctx.config.sandbox``."""
        from codebase.context import SandboxRuntime
        from core.sandbox.schemas import StartSessionRequest

        repo_dir = tmp_path / "repoX"
        repo_dir.mkdir(parents=True)
        (repo_dir / "README.md").write_text("hello")

        # Build a runtime whose ``ctx.sandbox`` carries the authoritative values,
        # and whose ``ctx.config.sandbox.*`` would *not* satisfy the assertions
        # (None base_image would actually trip StartSessionRequest validation).
        runtime = Mock()
        runtime.context = Mock()
        runtime.context.gitrepo = Mock(working_dir=str(repo_dir))
        runtime.context.config = Mock()
        runtime.context.config.sandbox = Mock()
        runtime.context.config.sandbox.base_image = None  # Would fail if read.
        runtime.context.config.sandbox.memory_bytes = None
        runtime.context.config.sandbox.cpus = None
        egress = EgressConfigRequest(policy=EgressPolicy(default="allow"))
        runtime.context.sandbox = SandboxRuntime(
            base_image="alpine:test", egress=egress, memory_bytes=1_234, cpus=2.5, env_vars={"X": "y"}
        )

        captured: dict = {}

        async def fake_start_session(req: StartSessionRequest) -> str:
            captured["req"] = req
            return "sess_ctx"

        client = MagicMock()
        client.start_session = AsyncMock(side_effect=fake_start_session)
        client.seed_session = AsyncMock()
        with (
            patch("automation.agent.middlewares.sandbox._make_repo_archive", return_value=b""),
            patch("automation.agent.middlewares.sandbox._make_global_skills_archive", return_value=None),
        ):
            middleware = SandboxMiddleware(
                agent_root="/repoX", client=client, sandbox_backend=SandboxFileBackend(client=client)
            )
            update = await middleware.abefore_agent({}, runtime)

        assert update == {"session_id": "sess_ctx"}
        req = captured["req"]
        assert isinstance(req, StartSessionRequest)
        assert req.base_image == "alpine:test"
        assert req.egress == egress
        assert req.memory_bytes == 1_234
        assert req.cpus == 2.5
        assert req.environment == {"X": "y"}


def test_sandbox_prompt_uses_workspace_paths():
    """The bash + scratchpad prompt blocks point at the /workspace layout (not /repo, /scratch)."""
    from automation.agent.middlewares.sandbox import BASH_TOOL_DESCRIPTION, SANDBOX_SYSTEM_PROMPT

    assert "/workspace/tmp" in SANDBOX_SYSTEM_PROMPT
    assert "/scratch" not in SANDBOX_SYSTEM_PROMPT
    assert "/workspace/repo" in BASH_TOOL_DESCRIPTION
    assert "/repos/" not in BASH_TOOL_DESCRIPTION


class TestSessionExists:
    """The liveness check that gates state-based warm reuse in ``abefore_agent``."""

    async def test_returns_client_result_when_alive(self):
        client = Mock(session_exists=AsyncMock(return_value=True))
        assert await SandboxMiddleware._session_exists(client, "warm-1") is True
        client.session_exists.assert_awaited_once_with("warm-1")

    async def test_returns_false_when_session_gone(self):
        client = Mock(session_exists=AsyncMock(return_value=False))
        assert await SandboxMiddleware._session_exists(client, "stale-1") is False

    async def test_soft_fails_to_false_on_transport_error(self):
        """A transient ``session_exists`` HTTP error falls back to a cold create rather than
        failing the run."""
        client = Mock(session_exists=AsyncMock(side_effect=httpx.HTTPError("boom")))
        assert await SandboxMiddleware._session_exists(client, "warm-1") is False


class TestSandboxEgress:
    def _mw(self, client) -> SandboxMiddleware:
        return SandboxMiddleware(
            agent_root="/workspace/repo", client=client, sandbox_backend=SandboxFileBackend(client=client)
        )

    @staticmethod
    @contextmanager
    def _patch_archives():
        """Stub the archive builders so the fresh-create path doesn't touch the filesystem."""
        with (
            patch("automation.agent.middlewares.sandbox._make_repo_archive", return_value=b""),
            patch("automation.agent.middlewares.sandbox._make_global_skills_archive", return_value=None),
        ):
            yield

    def _runtime_with_egress(self):
        runtime = _make_runtime()
        runtime.context.sandbox.egress = EgressConfigRequest(policy=EgressPolicy(default="allow"))
        return runtime, runtime.context.sandbox.egress

    async def test_no_configure_egress_on_fresh_create(self):
        """Egress is attached at start_session time (via egress= field); configure_egress is never called."""
        client = MagicMock()
        client.start_session = AsyncMock(return_value="sess-e")
        client.seed_session = AsyncMock()
        client.configure_egress = AsyncMock()
        runtime, _ = self._runtime_with_egress()
        with self._patch_archives():
            await self._mw(client).abefore_agent({}, runtime)
        client.configure_egress.assert_not_awaited()

    async def test_no_configure_egress_on_warm_reuse(self):
        """Warm reuse calls update_egress (not configure_egress) to refresh the credential."""
        client = MagicMock()
        client.session_exists = AsyncMock(return_value=True)
        client.start_session = AsyncMock()
        client.configure_egress = AsyncMock()
        client.update_egress = AsyncMock()
        runtime, _ = self._runtime_with_egress()
        result = await self._mw(client).abefore_agent({"session_id": "sess-warm"}, runtime)
        assert result == {"session_id": "sess-warm"}
        client.start_session.assert_not_awaited()
        client.configure_egress.assert_not_awaited()
        client.update_egress.assert_awaited_once()

    async def test_skips_egress_when_none(self):
        client = MagicMock()
        client.start_session = AsyncMock(return_value="sess-x")
        client.seed_session = AsyncMock()
        client.configure_egress = AsyncMock()
        runtime = _make_runtime()  # egress None — no egress configured
        with self._patch_archives():
            await self._mw(client).abefore_agent({}, runtime)
        client.configure_egress.assert_not_awaited()

    async def test_create_time_400_without_egress_detail_propagates_raw(self):
        """A create-time 400 that does NOT mention 'egress' is re-raised as-is (not mapped)."""
        resp = httpx.Response(400, json={"detail": "base_image is invalid"})
        client = MagicMock()
        client.start_session = AsyncMock(
            side_effect=httpx.HTTPStatusError("400", request=httpx.Request("POST", "x"), response=resp)
        )
        runtime, _ = self._runtime_with_egress()
        with pytest.raises(httpx.HTTPStatusError) as raised:
            await self._mw(client).abefore_agent({}, runtime)
        assert not isinstance(raised.value, SandboxEgressUnavailableError)

    async def test_start_passes_egress_block_and_no_separate_provision(self):
        """start_session is called with egress set; no separate configure_egress call is made."""
        client = MagicMock()
        client.start_session = AsyncMock(return_value="sess-egress")
        client.seed_session = AsyncMock()
        client.configure_egress = AsyncMock()
        runtime, egress = self._runtime_with_egress()
        with self._patch_archives():
            await self._mw(client).abefore_agent({}, runtime)
        sent = client.start_session.call_args.args[0]
        assert sent.egress is not None
        assert sent.egress == egress
        client.configure_egress.assert_not_awaited()

    async def test_create_time_400_egress_maps_to_unavailable(self):
        """A create-time 400 with an egress detail is mapped to SandboxEgressUnavailableError."""
        resp = httpx.Response(400, json={"detail": "egress requires the egress proxy, which is not configured"})
        client = MagicMock()
        client.start_session = AsyncMock(
            side_effect=httpx.HTTPStatusError("400", request=httpx.Request("POST", "x"), response=resp)
        )
        runtime, _ = self._runtime_with_egress()
        with pytest.raises(SandboxEgressUnavailableError):
            await self._mw(client).abefore_agent({}, runtime)


def _egress(token: str) -> EgressConfigRequest:
    return EgressConfigRequest(
        policy=EgressPolicy(rules=[EgressRule(host="gitlab.test", inject="platform")]),
        secrets={"platform": EgressSecret(header="Authorization", value=SecretStr(token))},
    )


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    (path / "README.md").write_text("hello\n")
    return path


_PROBE = ("true",)
_FRESH_SESSION_TURN = ["start_session", "seed_session", "run_commands", "close_session"]


async def _turn(client: FakeSandboxClient, state: dict, runtime: Mock, *, thread_id: str | None = "t-1") -> dict:
    """Run one agent turn — the sandbox hooks around a command through the bound backend — and return
    the state the checkpoint would hold after it."""
    backend = SandboxFileBackend(client=client)
    middleware = SandboxMiddleware(agent_root="/workspace/repo", client=client, sandbox_backend=backend)
    with patch("automation.agent.middlewares.sandbox.conversation_thread_id", return_value=thread_id):
        state = {**state, **(await middleware.abefore_agent(state, runtime) or {})}
        await backend.run_commands(list(_PROBE), fail_fast=True)
        return {**state, **(await middleware.aafter_agent(state, runtime) or {})}


def _refused_refresh(stale: str, egress: EgressConfigRequest) -> list[tuple]:
    """The calls that open a turn whose warm session can't take ``egress``: it is closed before anything starts."""
    return [("session_exists", (stale,)), ("update_egress", (stale, egress)), ("close_session", (stale, True))]


class TestPinnedSessionLifecycle:
    """Session lifecycle (B1–B6), asserted on the fake sandbox's state and call log and the checkpointed session id."""

    async def test_next_turn_reuses_the_warm_session(self, repo_dir: Path):
        """B1: the next turn reuses the checkpointed session via `session_exists`; nothing new is started or seeded."""
        client = FakeSandboxClient.opened()
        runtime = _make_agent_runtime(repo_dir)

        state = await _turn(client, {}, runtime)
        session_id = state["session_id"]
        assert client.sessions[session_id].repo_files == {"README.md"}
        assert any(name.endswith("SKILL.md") for name in client.sessions[session_id].skills_files)
        assert client.sessions[session_id].state == "stopped"
        mark = len(client.calls)

        state = await _turn(client, state, runtime)

        assert state["session_id"] == session_id
        assert client.calls[mark:] == [
            ("session_exists", (session_id,)),
            ("run_commands", (session_id, _PROBE)),
            ("close_session", (session_id, False)),
        ]

    async def test_a_reaped_session_is_replaced_by_a_fresh_one(self, repo_dir: Path):
        """B1: a checkpointed session the sandbox no longer has is replaced by a freshly seeded one."""
        client = FakeSandboxClient.opened()
        runtime = _make_agent_runtime(repo_dir)
        state = await _turn(client, {}, runtime)
        reaped = state["session_id"]
        del client.sessions[reaped]
        mark = len(client.calls)

        state = await _turn(client, state, runtime)

        assert state["session_id"] != reaped
        assert client.method_names()[mark:] == ["session_exists", *_FRESH_SESSION_TURN]
        assert client.calls_to("run_commands")[-1] == (state["session_id"], _PROBE)
        assert client.sessions[state["session_id"]].repo_files == {"README.md"}

    @pytest.mark.parametrize("status", [500, None])
    async def test_an_unconfirmed_session_is_replaced_and_the_possible_leak_logged(
        self, repo_dir: Path, status: int | None, caplog
    ):
        """B1: a non-404 `session_exists` error starts a fresh session and logs the old one as possibly leaked."""
        client = FakeSandboxClient.opened()
        runtime = _make_agent_runtime(repo_dir)
        state = await _turn(client, {}, runtime)
        stale = state["session_id"]
        client.fail("session_exists", status=status)

        with caplog.at_level("ERROR", logger="daiv.tools"):
            state = await _turn(client, state, runtime)

        assert state["session_id"] != stale
        assert client.calls_to("run_commands")[-1] == (state["session_id"], _PROBE)
        assert f"Could not validate sandbox session {stale}" in caplog.text

    async def test_a_reused_session_gets_the_fresh_credential(self, repo_dir: Path):
        """B2: a reused session gets this run's egress credential pushed onto it before the turn runs."""
        client = FakeSandboxClient.opened()
        state = await _turn(client, {}, _make_agent_runtime(repo_dir, egress=_egress("tok-1")))
        session_id = state["session_id"]
        fresh = _egress("tok-2")
        mark = len(client.calls)

        await _turn(client, state, _make_agent_runtime(repo_dir, egress=fresh))

        assert client.calls[mark:] == [
            ("session_exists", (session_id,)),
            ("update_egress", (session_id, fresh)),
            ("run_commands", (session_id, _PROBE)),
            ("close_session", (session_id, False)),
        ]
        assert client.sessions[session_id].egress == fresh

    @pytest.mark.parametrize("status", [404, 409, None])
    async def test_a_failed_credential_refresh_replaces_the_session(self, repo_dir: Path, status: int | None, caplog):
        """B2: when `update_egress` fails, the old container is force-closed before a new one is started and seeded."""
        client = FakeSandboxClient.opened()
        state = await _turn(client, {}, _make_agent_runtime(repo_dir, egress=_egress("tok-1")))
        stale = state["session_id"]
        client.fail("update_egress", status=status)
        fresh = _egress("tok-2")
        mark = len(client.calls)

        with caplog.at_level("WARNING", logger="daiv.tools"):
            state = await _turn(client, state, _make_agent_runtime(repo_dir, egress=fresh))

        assert client.calls[mark : mark + 3] == _refused_refresh(stale, fresh)
        assert client.method_names()[mark + 3 :] == _FRESH_SESSION_TURN
        assert stale not in client.sessions
        assert client.calls_to("run_commands")[-1] == (state["session_id"], _PROBE)
        assert client.sessions[state["session_id"]].request.egress == fresh
        assert client.sessions[state["session_id"]].repo_files == {"README.md"}
        assert f"Egress refresh failed for warm sandbox session {stale}" in caplog.text

    async def test_a_session_started_without_egress_is_replaced_once_egress_is_needed(self, repo_dir: Path):
        """B2: a warm session with no egress proxy can't take a credential, so it is closed and replaced."""
        client = FakeSandboxClient.opened()
        state = await _turn(client, {}, _make_agent_runtime(repo_dir))
        stale = state["session_id"]
        fresh = _egress("tok-1")
        mark = len(client.calls)

        state = await _turn(client, state, _make_agent_runtime(repo_dir, egress=fresh))

        assert client.calls[mark : mark + 3] == _refused_refresh(stale, fresh)
        assert client.method_names()[mark + 3 :] == _FRESH_SESSION_TURN
        assert stale not in client.sessions
        assert client.sessions[state["session_id"]].request.egress == fresh
        assert client.sessions[state["session_id"]].repo_files == {"README.md"}

    async def test_a_stale_session_that_will_not_close_is_logged_and_still_replaced(self, repo_dir: Path, caplog):
        """B2: a failed close of the stale session is logged and the replacement session still starts."""
        client = FakeSandboxClient.opened()
        state = await _turn(client, {}, _make_agent_runtime(repo_dir, egress=_egress("tok-1")))
        stale = state["session_id"]
        client.fail("update_egress", status=409)
        client.fail("close_session", status=500)

        with caplog.at_level("WARNING", logger="daiv.tools"):
            state = await _turn(client, state, _make_agent_runtime(repo_dir, egress=_egress("tok-2")))

        assert state["session_id"] != stale
        assert client.calls_to("run_commands")[-1] == (state["session_id"], _PROBE)
        assert f"Failed to stop stale sandbox session {stale}" in caplog.text

    async def test_a_missing_egress_proxy_fails_closed(self, repo_dir: Path):
        """B3: a create-time 400 naming the egress proxy raises `SandboxEgressUnavailableError`; nothing follows."""
        client = FakeSandboxClient.opened()
        client.fail("start_session", status=400, detail="egress requires the egress proxy, which is not configured")

        with pytest.raises(SandboxEgressUnavailableError):
            await _turn(client, {}, _make_agent_runtime(repo_dir, egress=_egress("tok-1")))

        assert client.method_names() == ["start_session"]

    async def test_any_other_create_time_400_is_re_raised(self, repo_dir: Path):
        """B3: a 400 that does not name the egress proxy propagates unchanged."""
        client = FakeSandboxClient.opened()
        client.fail("start_session", status=400, detail="base_image is invalid")

        with pytest.raises(httpx.HTTPStatusError) as raised:
            await _turn(client, {}, _make_agent_runtime(repo_dir))

        assert raised.value.response.status_code == 400
        assert client.method_names() == ["start_session"]

    async def test_a_seed_failure_removes_the_new_session(self, repo_dir: Path, caplog):
        """B4: a seed failure is logged, force-closes the just-created session, then re-raises."""
        client = FakeSandboxClient.opened()
        client.fail("seed_session", status=500)

        with caplog.at_level("ERROR", logger="daiv.tools"), pytest.raises(httpx.HTTPStatusError):
            await _turn(client, {}, _make_agent_runtime(repo_dir))

        assert client.sessions == {}
        assert client.calls_to("close_session") == [("sess-1", True)]
        assert "Failed to build or seed sandbox session sess-1" in caplog.text

    async def test_a_seed_failure_whose_cleanup_fails_still_raises_the_seed_error(self, repo_dir: Path, caplog):
        """B4: a failed cleanup close is logged, and the seed error — not the close error — propagates."""
        client = FakeSandboxClient.opened()
        client.fail("seed_session", status=500)
        client.fail("close_session", status=None)

        with caplog.at_level("ERROR", logger="daiv.tools"), pytest.raises(httpx.HTTPStatusError):
            await _turn(client, {}, _make_agent_runtime(repo_dir))

        assert client.calls_to("close_session") == [("sess-1", True)]
        assert "Failed to close session sess-1 after seed failure" in caplog.text

    async def test_a_resumable_run_stops_the_container_and_keeps_its_id(self, repo_dir: Path):
        """B5: a run with a thread id stops the container and leaves the id in state."""
        client = FakeSandboxClient.opened()

        state = await _turn(client, {}, _make_agent_runtime(repo_dir))

        assert client.calls_to("close_session") == [(state["session_id"], False)]
        assert client.sessions[state["session_id"]].state == "stopped"

    async def test_a_one_shot_run_removes_the_container(self, repo_dir: Path):
        """B5: a run without a thread id force-removes the container and clears the id from state."""
        client = FakeSandboxClient.opened()

        state = await _turn(client, {}, _make_agent_runtime(repo_dir), thread_id=None)

        assert client.calls_to("close_session") == [("sess-1", True)]
        assert client.sessions == {}
        assert state["session_id"] is None

    @pytest.mark.parametrize(("status", "leak_logged"), [(404, False), (409, False), (500, True), (None, True)])
    @pytest.mark.parametrize("thread_id", ["t-1", None])
    async def test_a_failed_close_is_logged_not_raised(
        self, repo_dir: Path, status: int | None, leak_logged: bool, thread_id, caplog
    ):
        """B5: close errors never fail the run; any but 404/409 are logged as a possibly leaked container."""
        client = FakeSandboxClient.opened()
        client.fail("close_session", status=status)

        with caplog.at_level("ERROR", logger="daiv.tools"):
            await _turn(client, {}, _make_agent_runtime(repo_dir), thread_id=thread_id)

        assert client.calls_to("close_session") == [("sess-1", thread_id is None)]
        assert ("sess-1 close" in caplog.text and "may have leaked" in caplog.text) is leak_logged

    async def test_a_subagent_shares_the_parent_session(self, repo_dir: Path):
        """B6: a subagent's sandbox hooks never open, refresh or close a session."""
        client = FakeSandboxClient.opened()
        runtime = _make_agent_runtime(repo_dir, egress=_egress("tok-1"))
        backend = SandboxFileBackend(client=client)
        parent = SandboxMiddleware(agent_root="/workspace/repo", client=client, sandbox_backend=backend)
        state = await parent.abefore_agent({}, runtime)
        calls_before = list(client.calls)

        subagent = SandboxMiddleware(
            agent_root="/workspace/repo", client=client, sandbox_backend=backend, close_session=False
        )
        await subagent.abefore_agent(state, runtime)
        await subagent.aafter_agent(state, runtime)

        assert client.calls == calls_before
        assert client.sessions[state["session_id"]].state == "running"
        await backend.run_commands(["true"], fail_fast=True)
        assert client.calls_to("run_commands") == [(state["session_id"], ("true",))]
