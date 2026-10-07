import io
import json
import tarfile
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from git import Repo

from automation.agent.middlewares.sandbox import (
    SANDBOX_SYSTEM_PROMPT,
    BashFailure,
    SandboxMiddleware,
    _run_bash_commands,
    acquire_sandbox,
)
from automation.agent.workspace.sandbox import SandboxWorkspace
from automation.agent.workspace.session import SandboxEgressUnavailableError, SandboxSession
from codebase.clients.base import GitEgressCredential
from core.conf import settings as core_settings
from core.sandbox.egress import with_platform_credential
from core.sandbox.schemas import EgressConfigRequest, RunCommandResult, RunCommandsResponse
from tests.unit_tests.conftest import (
    FakeSandboxClient,
    FakeWorkspace,
    acquired_session,
    sandbox_backend_on,
    sandbox_spec,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sandbox_envs.spec import SandboxSpec


def _make_agent_runtime(repo_working_dir: str | Path, *, spec: SandboxSpec | None = None) -> Mock:
    runtime = Mock()
    runtime.context = Mock()
    runtime.context.gitrepo = Mock(working_dir=str(repo_working_dir))
    runtime.context.sandbox = spec or sandbox_spec()
    return runtime


def _make_bash_runtime(repo: Repo) -> Mock:
    """Build a ToolRuntime-compatible mock for bash_tool tests."""
    from langchain.tools import ToolRuntime

    runtime = ToolRuntime(
        state={"session_id": "sess_1"},
        context=Mock(gitrepo=repo, sandbox=sandbox_spec()),
        config={},
        stream_writer=Mock(),
        tool_call_id="call_1",
        store=None,
    )
    return runtime


def _make_middleware() -> SandboxMiddleware:
    """A SandboxMiddleware over a session nothing acquired, for tests of the bash policy and the system prompt."""
    return SandboxMiddleware(
        agent_root="/dummy", workspace=SandboxWorkspace(SandboxSession(FakeSandboxClient(), sandbox_spec()))
    )


def _bash_tool_with_fake_client(client: Mock):
    """The bash tool of a SandboxMiddleware over an acquired session on ``client``."""
    workspace = SandboxWorkspace(acquired_session(client, "sess_1"))
    return SandboxMiddleware(agent_root="/dummy", workspace=workspace).tools[0]


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

    def test_a_workspace_without_a_shell_is_refused(self):
        """A disk workspace has no shell to back the tool, so the middleware refuses it when it is built rather than on
        the first command."""
        with pytest.raises(ValueError, match="a shell and a sandbox session"):
            SandboxMiddleware(agent_root="/dummy", workspace=FakeWorkspace())


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

    async def test_a_committing_merge_is_blocked_with_the_no_commit_form_to_use(self, tmp_path: Path):
        """The generic hint says not to try alternatives; here the agent must retry with `--no-commit`."""
        output, run_mock = await self._invoke("git merge origin/main", tmp_path)
        assert "git merge --no-commit --no-ff" in output
        assert "do not rephrase" not in output
        run_mock.assert_not_awaited()

    async def test_a_branch_switch_is_blocked_with_git_restore_to_use(self, tmp_path: Path):
        output, run_mock = await self._invoke("git checkout main", tmp_path)
        assert "git restore" in output
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
        backend = sandbox_backend_on(Mock(), "sess_1")
        backend.run_commands = AsyncMock(return_value=RunCommandsResponse(results=[]))

        response = await _run_bash_commands(backend, ["echo ok"])

        assert response is not None
        backend.run_commands.assert_awaited_once()
        assert backend.run_commands.await_args.args[0] == ["echo ok"]
        assert backend.run_commands.await_args.kwargs["fail_fast"] is True

    async def _run_with_error(self, error: Exception) -> object:
        backend = sandbox_backend_on(Mock(), "sess_1")
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
    async def test_a_fresh_session_is_seeded_with_the_repo_and_the_global_skills(self, tmp_path: Path):
        repo_dir = tmp_path / "repoX"
        repo_dir.mkdir(parents=True)
        (repo_dir / "README.md").write_text("hello")
        builtin = tmp_path / "builtin"
        (builtin / "skill-one").mkdir(parents=True)
        (builtin / "skill-one" / "SKILL.md").write_text("hi")
        client = FakeSandboxClient.opened()

        with (
            patch("automation.agent.middlewares.sandbox.BUILTIN_SKILLS_PATH", builtin),
            patch("automation.agent.middlewares.sandbox.agent_settings") as settings,
        ):
            settings.CUSTOM_SKILLS_PATH = None
            state = await _turn(client, {}, _make_agent_runtime(repo_dir))

        seeded = client.sessions[state["session_id"]]
        assert seeded.repo_files == {"README.md"}
        assert seeded.skills_files == {"skill-one/SKILL.md"}

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


def test_sandbox_prompt_uses_workspace_paths():
    """The bash + scratchpad prompt blocks point at the /workspace layout (not /repo, /scratch)."""
    from automation.agent.middlewares.sandbox import BASH_TOOL_DESCRIPTION, SANDBOX_SYSTEM_PROMPT

    assert "/workspace/tmp" in SANDBOX_SYSTEM_PROMPT
    assert "/scratch" not in SANDBOX_SYSTEM_PROMPT
    assert "/workspace/repo" in BASH_TOOL_DESCRIPTION
    assert "/repos/" not in BASH_TOOL_DESCRIPTION


_GIT_HOST = "gitlab.test"
_PROBE = ("true",)
_FRESH_SESSION_TURN = ["start_session", "seed_session", "run_commands", "close_session"]


def _egress(token: str) -> EgressConfigRequest:
    """The egress a network-off run holding push token ``token`` is provisioned with: the git host alone."""
    credential = GitEgressCredential.for_token(host=_GIT_HOST, token=token)
    return with_platform_credential(None, host=credential.host, header=credential.header, token=credential.value)


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    (path / "README.md").write_text("hello\n")
    return path


def _run_session(client: FakeSandboxClient, runtime: Mock, token: str | None) -> SandboxSession:
    """The session the executor builds for one turn; ``token`` is the push token the run mints."""
    credential = GitEgressCredential.for_token(host=_GIT_HOST, token=token) if token is not None else None
    return SandboxSession(client, runtime.context.sandbox, credential_source=AsyncMock(return_value=credential))


async def _turn(
    client: FakeSandboxClient, state: dict, runtime: Mock, *, token: str | None = None, thread_id: str | None = "t-1"
) -> dict:
    """Run one agent turn: the executor's acquisition from the last checkpoint, the sandbox hook that records it, a
    command through the backend, then the executor's release. Return the state the checkpoint would hold after it."""
    session = _run_session(client, runtime, token)
    workspace = SandboxWorkspace(session)
    middleware = SandboxMiddleware(agent_root="/workspace/repo", workspace=workspace)
    await acquire_sandbox(session, runtime.context, state)
    state = {**state, **(await middleware.abefore_agent(state, runtime) or {})}
    await workspace.bash.run_commands(list(_PROBE), fail_fast=True)
    await session.release(resumable=thread_id is not None)
    return state


def _refused_refresh(stale: str, egress: EgressConfigRequest) -> list[tuple]:
    """The calls that open a turn whose warm session can't take ``egress``: it is closed before anything starts."""
    return [("session_exists", (stale,)), ("update_egress", (stale, egress)), ("close_session", (stale, True))]


class TestPinnedSessionLifecycle:
    """Session lifecycle (B1–B6, B12), asserted on the fake sandbox's state and call log and the checkpointed state."""

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
        runtime = _make_agent_runtime(repo_dir)
        state = await _turn(client, {}, runtime, token="tok-1")  # noqa: S106
        session_id = state["session_id"]
        fresh = _egress("tok-2")
        mark = len(client.calls)

        await _turn(client, state, runtime, token="tok-2")  # noqa: S106

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
        runtime = _make_agent_runtime(repo_dir)
        state = await _turn(client, {}, runtime, token="tok-1")  # noqa: S106
        stale = state["session_id"]
        client.fail("update_egress", status=status)
        fresh = _egress("tok-2")
        mark = len(client.calls)

        with caplog.at_level("WARNING", logger="daiv.tools"):
            state = await _turn(client, state, runtime, token="tok-2")  # noqa: S106

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
        runtime = _make_agent_runtime(repo_dir)
        state = await _turn(client, {}, runtime)
        stale = state["session_id"]
        fresh = _egress("tok-1")
        mark = len(client.calls)

        state = await _turn(client, state, runtime, token="tok-1")  # noqa: S106

        assert client.calls[mark : mark + 3] == _refused_refresh(stale, fresh)
        assert client.method_names()[mark + 3 :] == _FRESH_SESSION_TURN
        assert stale not in client.sessions
        assert client.sessions[state["session_id"]].request.egress == fresh
        assert client.sessions[state["session_id"]].repo_files == {"README.md"}

    async def test_a_stale_session_that_will_not_close_is_logged_and_still_replaced(self, repo_dir: Path, caplog):
        """B2: a failed close of the stale session is logged and the replacement session still starts."""
        client = FakeSandboxClient.opened()
        runtime = _make_agent_runtime(repo_dir)
        state = await _turn(client, {}, runtime, token="tok-1")  # noqa: S106
        stale = state["session_id"]
        client.fail("update_egress", status=409)
        client.fail("close_session", status=500)

        with caplog.at_level("WARNING", logger="daiv.tools"):
            state = await _turn(client, state, runtime, token="tok-2")  # noqa: S106

        assert state["session_id"] != stale
        assert client.calls_to("run_commands")[-1] == (state["session_id"], _PROBE)
        assert f"Failed to close sandbox session {stale} after egress refresh failure" in caplog.text

    async def test_a_missing_egress_proxy_fails_closed(self, repo_dir: Path):
        """B3: a create-time 400 naming the egress proxy raises `SandboxEgressUnavailableError`; nothing follows."""
        client = FakeSandboxClient.opened()
        client.fail("start_session", status=400, detail="egress requires the egress proxy, which is not configured")

        with pytest.raises(SandboxEgressUnavailableError):
            await _turn(client, {}, _make_agent_runtime(repo_dir), token="tok-1")  # noqa: S106

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
        assert "Failed to close sandbox session sess-1 after an interrupted seed" in caplog.text

    async def test_a_resumable_run_stops_the_container_and_keeps_its_id(self, repo_dir: Path):
        """B5: a run with a thread id stops the container and leaves the id in state."""
        client = FakeSandboxClient.opened()

        state = await _turn(client, {}, _make_agent_runtime(repo_dir))

        assert client.calls_to("close_session") == [(state["session_id"], False)]
        assert client.sessions[state["session_id"]].state == "stopped"

    async def test_a_one_shot_run_removes_the_container(self, repo_dir: Path):
        """B5: a run without a thread id force-removes the container."""
        client = FakeSandboxClient.opened()

        await _turn(client, {}, _make_agent_runtime(repo_dir), thread_id=None)

        assert client.calls_to("close_session") == [("sess-1", True)]
        assert client.sessions == {}

    async def test_a_subagent_shares_the_parent_session(self, repo_dir: Path):
        """B6: a subagent's sandbox hook never opens, refreshes or closes a session."""
        client = FakeSandboxClient.opened()
        runtime = _make_agent_runtime(repo_dir)
        workspace = SandboxWorkspace(_run_session(client, runtime, "tok-1"))
        parent = SandboxMiddleware(agent_root="/workspace/repo", workspace=workspace)
        await acquire_sandbox(workspace.session, runtime.context, {})
        state = await parent.abefore_agent({}, runtime)
        calls_before = list(client.calls)

        subagent = SandboxMiddleware(agent_root="/workspace/repo", workspace=workspace)
        assert await subagent.abefore_agent(state, runtime) is None

        assert client.calls == calls_before
        assert client.sessions[state["session_id"]].state == "running"
        await workspace.bash.run_commands(["true"], fail_fast=True)
        assert client.calls_to("run_commands") == [(state["session_id"], ("true",))]

    async def test_a_session_started_for_another_environment_is_replaced(self, repo_dir: Path):
        """A warm session whose recorded spec fingerprint differs from this run's is removed, without waking it first,
        and replaced."""
        client = FakeSandboxClient.opened()
        state = await _turn(client, {}, _make_agent_runtime(repo_dir))
        stale = state["session_id"]
        changed = _make_agent_runtime(repo_dir, spec=sandbox_spec(base_image="python:3.13"))
        mark = len(client.calls)

        state = await _turn(client, state, changed)

        assert client.calls[mark] == ("close_session", (stale, True))
        assert client.method_names()[mark + 1 :] == _FRESH_SESSION_TURN
        assert stale not in client.sessions
        assert client.sessions[state["session_id"]].request.base_image == "python:3.13"
        assert state["sandbox_fingerprint"] == changed.context.sandbox.fingerprint

    async def test_a_session_recorded_before_fingerprints_is_reused_and_records_this_runs(self, repo_dir: Path):
        """B12: a checkpoint without a fingerprint reuses its session, and records this run's so a later edit counts."""
        client = FakeSandboxClient.opened()
        legacy = {"session_id": (await _turn(client, {}, _make_agent_runtime(repo_dir)))["session_id"]}
        changed = _make_agent_runtime(repo_dir, spec=sandbox_spec(base_image="python:3.13"))
        mark = len(client.calls)

        state = await _turn(client, legacy, changed)

        assert state["session_id"] == legacy["session_id"]
        assert client.method_names()[mark:] == ["session_exists", "run_commands", "close_session"]
        assert state["sandbox_fingerprint"] == changed.context.sandbox.fingerprint
