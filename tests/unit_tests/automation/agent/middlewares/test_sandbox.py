import io
import json
import tarfile
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

from git import Repo

from automation.agent.middlewares.sandbox import SANDBOX_SYSTEM_PROMPT, SandboxMiddleware, _run_bash_commands
from core.conf import settings as core_settings
from core.sandbox.client import DAIVSandboxClient
from core.sandbox.schemas import RunCommandResult, RunCommandsResponse

if TYPE_CHECKING:
    from pathlib import Path


def _make_sandbox_config_mock(disallow=(), allow=()):
    """Create a mock sandbox config with command policy."""
    config = Mock()
    config.sandbox = Mock()
    config.sandbox.base_image = "python:3.12"
    config.sandbox.network_enabled = False
    config.sandbox.memory_bytes = None
    config.sandbox.cpus = None
    config.sandbox.command_policy = Mock()
    config.sandbox.command_policy.disallow = disallow
    config.sandbox.command_policy.allow = allow
    return config


def _make_sandbox_runtime(disallow=(), allow=()):
    """Build a ``SandboxRuntime`` matching the legacy ``_make_sandbox_config_mock`` defaults."""
    from codebase.context import SandboxRuntime
    from core.sandbox.command_policy import SandboxCommandPolicy

    return SandboxRuntime(
        base_image="python:3.12",
        network_enabled=False,
        memory_bytes=None,
        cpus=None,
        env_vars={},
        command_policy=SandboxCommandPolicy(disallow=tuple(disallow), allow=tuple(allow)),
    )


def _make_agent_runtime(*, repo_working_dir: str) -> Mock:
    runtime = Mock()
    runtime.context = Mock()
    runtime.context.gitrepo = Mock(working_dir=repo_working_dir)
    runtime.context.config = _make_sandbox_config_mock()
    runtime.context.sandbox = _make_sandbox_runtime()
    return runtime


def _make_bash_runtime(repo: Repo, disallow=(), allow=()) -> Mock:
    """Build a ToolRuntime-compatible mock for bash_tool tests."""
    from langchain.tools import ToolRuntime

    runtime = ToolRuntime(
        state={"session_id": "sess_1"},
        context=Mock(
            gitrepo=repo,
            config=_make_sandbox_config_mock(disallow=disallow, allow=allow),
            sandbox=_make_sandbox_runtime(disallow=disallow, allow=allow),
        ),
        config={},
        stream_writer=Mock(),
        tool_call_id="call_1",
        store=None,
    )
    return runtime


def _make_middleware(*, close_session: bool = True) -> SandboxMiddleware:
    """Build a SandboxMiddleware with a dummy backend; tests that don't exercise
    write-sync never read it."""
    return SandboxMiddleware(backend=Mock(), agent_root="/dummy", close_session=close_session)


def _bash_tool_with_fake_client(client: Mock):
    """Build a fresh SandboxMiddleware with ``client`` pre-installed and return its bash tool."""
    middleware = _make_middleware()
    middleware._client = client
    return middleware.tools[0]


class TestBindBackend:
    def test_bind_backend_binds_sandbox_file_backend(self):
        from automation.agent.middlewares.file_system import SandboxFileBackend

        backend = SandboxFileBackend()
        mw = SandboxMiddleware(backend=backend, agent_root="/workspace/repo")
        mw._client = AsyncMock()

        mw._bind_backend("sid")
        assert backend._session_id == "sid"

        # Subagents share the parent's backend: re-binding the same session must not raise.
        mw._bind_backend("sid")
        assert backend._session_id == "sid"

    def test_bind_backend_noop_for_non_sandbox_backend(self):
        backend = Mock()  # a disk-backed / composite backend is not a SandboxFileBackend
        mw = SandboxMiddleware(backend=backend, agent_root="/x")
        mw._client = AsyncMock()

        mw._bind_backend("sid")
        backend.bind.assert_not_called()

    def test_bind_backend_subagent_reuses_parent_session_with_own_client(self):
        """Regression: parent and subagent share ONE backend, but each middleware has its OWN
        client. The subagent re-binds the parent's session through its own client — this must
        not raise (the session, not the client, identifies the workspace)."""
        from automation.agent.middlewares.file_system import SandboxFileBackend

        backend = SandboxFileBackend()
        parent = SandboxMiddleware(backend=backend, agent_root="/workspace/repo")
        parent._client = AsyncMock()
        parent._bind_backend("sid")

        subagent = SandboxMiddleware(backend=backend, agent_root="/workspace/repo", close_session=False)
        subagent._client = AsyncMock()  # distinct client object
        subagent._bind_backend("sid")  # must not raise

        assert backend._client is subagent._client
        assert backend._session_id == "sid"


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

    async def test_bash_tool_returns_error_when_sandbox_call_fails(self, tmp_path: Path):
        import httpx

        repo_dir = tmp_path / "repoX"
        repo_dir.mkdir(parents=True)
        repo = Repo.init(repo_dir)

        runtime = _make_bash_runtime(repo)
        client = Mock()
        client.run_commands = AsyncMock(side_effect=httpx.RequestError("boom"))
        bash_tool = _bash_tool_with_fake_client(client)

        output = await bash_tool.coroutine(command="echo ok", runtime=runtime)

        assert output.startswith("error: Sandbox call failed")

    async def test_bash_tool_raises_when_client_not_opened(self, tmp_path: Path):
        """Calling the bash tool before ``abefore_agent`` opens the client must fail loud."""
        import pytest

        repo_dir = tmp_path / "repoX"
        repo_dir.mkdir(parents=True)
        repo = Repo.init(repo_dir)

        runtime = _make_bash_runtime(repo)
        middleware = _make_middleware(close_session=True)
        bash_tool = middleware.tools[0]

        with pytest.raises(RuntimeError, match="bash tool invoked before abefore_agent"):
            await bash_tool.coroutine(command="echo ok", runtime=runtime)


class TestBashToolPolicyEnforcement:
    """
    Verify that bash_tool enforces the command policy before any sandbox call.
    All tests assert that _run_bash_commands is NOT called when a command is blocked.
    """

    async def _invoke(self, command: str, tmp_path: Path, extra_disallow=(), extra_allow=()):
        """Run bash_tool with a fresh repo and configurable policy."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        repo = Repo.init(repo_dir)

        runtime = _make_bash_runtime(repo, disallow=extra_disallow, allow=extra_allow)
        runtime.tool_call_id = "call_policy"
        runtime.state["session_id"] = "sess_policy"

        client = Mock()
        run_mock = AsyncMock(return_value=RunCommandsResponse(results=[]))
        client.run_commands = run_mock
        bash_tool = _bash_tool_with_fake_client(client)

        with (
            patch.object(core_settings, "SANDBOX_COMMAND_POLICY_DISALLOW", ()),
            patch.object(core_settings, "SANDBOX_COMMAND_POLICY_ALLOW", ()),
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

    # --- Parse failure → fail-closed ---

    async def test_unmatched_quote_blocks_execution(self, tmp_path: Path):
        output, run_mock = await self._invoke('echo "unclosed', tmp_path)
        assert output.startswith("error:")
        assert "parse" in output.lower()
        run_mock.assert_not_awaited()

    # --- Repo-level policy ---

    async def test_repo_disallow_blocks_custom_command(self, tmp_path: Path):
        output, run_mock = await self._invoke("danger cmd", tmp_path, extra_disallow=("danger cmd",))
        assert output.startswith("error:")
        assert "repo_disallow" in output
        run_mock.assert_not_awaited()

    async def test_repo_allow_does_not_override_default_disallow(self, tmp_path: Path):
        """Even if repo.allow contains 'git commit', it stays blocked."""
        output, run_mock = await self._invoke("git commit -m x", tmp_path, extra_allow=("git commit",))
        assert output.startswith("error:")
        assert "default_disallow" in output
        run_mock.assert_not_awaited()

    # --- Denial message format ---

    async def test_denial_message_contains_reason_category(self, tmp_path: Path):
        output, _ = await self._invoke("git push", tmp_path)
        assert "default_disallow" in output

    async def test_denial_message_contains_matched_rule(self, tmp_path: Path):
        output, _ = await self._invoke("git push", tmp_path)
        assert "git push" in output


class TestRunBashCommands:
    async def test_run_bash_commands_no_archive_field(self):
        """_run_bash_commands no longer tarballs the working dir; archive is gone from RunCommandsRequest."""
        run_commands_mock = AsyncMock(return_value=RunCommandsResponse(results=[]))
        client = Mock()
        client.run_commands = run_commands_mock

        response = await _run_bash_commands(client, ["echo ok"], "sess_1")

        assert response is not None
        run_commands_mock.assert_awaited_once()
        _session_id, request = run_commands_mock.call_args.args

        # The RunCommandsRequest schema no longer has an archive field.
        dumped = request.model_dump()
        assert "archive" not in dumped
        assert dumped["commands"] == ["echo ok"]


class TestSandboxMiddleware:
    @staticmethod
    def _patch_client_lifecycle():
        """Stub ``DAIVSandboxClient.open``/``close`` so no real httpx.AsyncClient is created."""
        return (
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.open", new=AsyncMock(return_value=None)),
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.close", new=AsyncMock(return_value=None)),
        )

    async def test_abefore_agent_starts_session_and_sets_session_id(self, tmp_path: Path):
        repo_dir = tmp_path / "repoX"
        repo_dir.mkdir(parents=True)
        (repo_dir / "README.md").write_text("hello")
        runtime = _make_agent_runtime(repo_working_dir=str(repo_dir))

        # No global skills available (empty builtin dir, no custom) -> skills_archive is None.
        empty_builtin = tmp_path / "builtin-empty"
        empty_builtin.mkdir()

        open_patch, close_patch = self._patch_client_lifecycle()
        with (
            open_patch,
            close_patch,
            patch("automation.agent.middlewares.sandbox.BUILTIN_SKILLS_PATH", empty_builtin),
            patch("automation.agent.middlewares.sandbox.agent_settings") as settings,
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.start_session",
                new=AsyncMock(return_value="sess_1"),
            ) as start_session_mock,
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.seed_session", new=AsyncMock(return_value=None)
            ) as seed_session_mock,
        ):
            settings.CUSTOM_SKILLS_PATH = None
            middleware = _make_middleware(close_session=True)
            update = await middleware.abefore_agent({}, runtime)

        assert update == {"session_id": "sess_1"}
        start_session_mock.assert_awaited_once()
        seed_session_mock.assert_awaited_once()
        _args, kwargs = seed_session_mock.call_args
        assert isinstance(kwargs.get("repo_archive"), (bytes, bytearray))
        assert kwargs.get("skills_archive") is None
        assert middleware._client is not None

    async def test_abefore_agent_closes_session_on_seed_failure(self, tmp_path: Path):
        """If seed_session raises, the started session is closed and the client is released (no leak)."""
        import pytest

        repo_dir = tmp_path / "repoX"
        repo_dir.mkdir(parents=True)
        (repo_dir / "README.md").write_text("hello")
        runtime = _make_agent_runtime(repo_working_dir=str(repo_dir))

        close_session_mock = AsyncMock(return_value=None)
        client_close_mock = AsyncMock(return_value=None)

        with (
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.open", new=AsyncMock(return_value=None)),
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.close", new=client_close_mock),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.start_session",
                new=AsyncMock(return_value="sess_leaky"),
            ),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.seed_session",
                new=AsyncMock(side_effect=RuntimeError("simulated seed failure")),
            ),
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.close_session", new=close_session_mock),
        ):
            middleware = _make_middleware(close_session=True)
            with pytest.raises(RuntimeError, match="simulated seed failure"):
                await middleware.abefore_agent({}, runtime)

        close_session_mock.assert_awaited_once_with("sess_leaky")
        client_close_mock.assert_awaited_once()
        assert middleware._client is None

    async def test_abefore_agent_releases_client_on_start_session_failure(self, tmp_path: Path):
        """If start_session raises (sandbox unreachable / 5xx), the just-opened client is released."""
        import pytest

        repo_dir = tmp_path / "repoX"
        repo_dir.mkdir(parents=True)
        runtime = _make_agent_runtime(repo_working_dir=str(repo_dir))

        close_session_mock = AsyncMock(return_value=None)
        client_close_mock = AsyncMock(return_value=None)

        with (
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.open", new=AsyncMock(return_value=None)),
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.close", new=client_close_mock),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.start_session",
                new=AsyncMock(side_effect=RuntimeError("sandbox unreachable")),
            ),
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.close_session", new=close_session_mock),
        ):
            middleware = _make_middleware(close_session=True)
            with pytest.raises(RuntimeError, match="sandbox unreachable"):
                await middleware.abefore_agent({}, runtime)

        # No session was started, so close_session must NOT be called.
        close_session_mock.assert_not_awaited()
        # But the client we opened must be released.
        client_close_mock.assert_awaited_once()
        assert middleware._client is None

    async def test_aafter_agent_releases_client_when_close_session_raises_unexpected(self, tmp_path: Path):
        """``finally`` releases the client even when close_session raises an unhandled exception."""
        import pytest

        runtime = _make_agent_runtime(repo_working_dir=str(tmp_path / "repoX"))
        state = {"session_id": "sess_1"}

        client_close_mock = AsyncMock(return_value=None)
        with (
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.open", new=AsyncMock(return_value=None)),
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.close", new=client_close_mock),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.close_session",
                new=AsyncMock(side_effect=RuntimeError("unexpected")),
            ),
        ):
            middleware = _make_middleware(close_session=True)
            middleware._client = DAIVSandboxClient()
            with pytest.raises(RuntimeError, match="unexpected"):
                await middleware.aafter_agent(state, runtime)

        client_close_mock.assert_awaited_once()
        assert middleware._client is None

    async def test_abefore_agent_reuses_session_id_when_close_session_false(self, tmp_path: Path):
        runtime = _make_agent_runtime(repo_working_dir=str(tmp_path / "repoX"))
        state = {"session_id": "sess_existing"}

        open_patch, close_patch = self._patch_client_lifecycle()
        with (
            open_patch,
            close_patch,
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.start_session",
                new=AsyncMock(return_value="sess_1"),
            ) as start_session_mock,
        ):
            middleware = _make_middleware(close_session=False)
            update = await middleware.abefore_agent(state, runtime)

        assert update is None
        start_session_mock.assert_not_awaited()
        assert middleware._client is not None

    async def test_aafter_agent_closes_session_and_clears_session_id(self, tmp_path: Path):
        runtime = _make_agent_runtime(repo_working_dir=str(tmp_path / "repoX"))
        state = {"session_id": "sess_1"}

        client_close_mock = AsyncMock(return_value=None)
        with (
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.open", new=AsyncMock(return_value=None)),
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.close", new=client_close_mock),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.close_session", new=AsyncMock(return_value=None)
            ) as close_session_mock,
        ):
            middleware = _make_middleware(close_session=True)
            middleware._client = DAIVSandboxClient()
            update = await middleware.aafter_agent(state, runtime)

        assert update == {"session_id": None}
        close_session_mock.assert_awaited_once_with("sess_1")
        client_close_mock.assert_awaited_once()
        assert middleware._client is None

    async def test_aafter_agent_does_not_close_session_when_close_session_false(self, tmp_path: Path):
        runtime = _make_agent_runtime(repo_working_dir=str(tmp_path / "repoX"))
        state = {"session_id": "sess_1"}

        client_close_mock = AsyncMock(return_value=None)
        with (
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.open", new=AsyncMock(return_value=None)),
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.close", new=client_close_mock),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.close_session", new=AsyncMock(return_value=None)
            ) as close_session_mock,
        ):
            middleware = _make_middleware(close_session=False)
            middleware._client = DAIVSandboxClient()
            update = await middleware.aafter_agent(state, runtime)

        assert update is None
        close_session_mock.assert_not_awaited()
        # Subagent path: session is the parent's, but the client we opened still must be released.
        client_close_mock.assert_awaited_once()
        assert middleware._client is None

    async def test_abefore_agent_passes_skills_archive_when_skills_dir_populated(self, tmp_path: Path):
        repo_dir = tmp_path / "repoX"
        repo_dir.mkdir(parents=True)
        (repo_dir / "README.md").write_text("hello")

        builtin = tmp_path / "builtin"
        (builtin / "skill-one").mkdir(parents=True)
        (builtin / "skill-one" / "SKILL.md").write_text("hi")

        runtime = _make_agent_runtime(repo_working_dir=str(repo_dir))

        open_patch, close_patch = self._patch_client_lifecycle()
        with (
            open_patch,
            close_patch,
            patch("automation.agent.middlewares.sandbox.BUILTIN_SKILLS_PATH", builtin),
            patch("automation.agent.middlewares.sandbox.agent_settings") as settings,
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.start_session",
                new=AsyncMock(return_value="sess_skills"),
            ),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.seed_session", new=AsyncMock(return_value=None)
            ) as seed_session_mock,
        ):
            settings.CUSTOM_SKILLS_PATH = None
            middleware = SandboxMiddleware(backend=Mock(), agent_root=f"/{repo_dir.name}", close_session=True)
            update = await middleware.abefore_agent({}, runtime)

        assert update == {"session_id": "sess_skills"}
        seed_session_mock.assert_awaited_once()
        _args, kwargs = seed_session_mock.call_args
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
        from core.sandbox.command_policy import SandboxCommandPolicy
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
        runtime.context.config.sandbox.network_enabled = None
        runtime.context.config.sandbox.memory_bytes = None
        runtime.context.config.sandbox.cpus = None
        runtime.context.sandbox = SandboxRuntime(
            base_image="alpine:test",
            network_enabled=True,
            memory_bytes=1_234,
            cpus=2.5,
            env_vars={"X": "y"},
            command_policy=SandboxCommandPolicy(),
        )

        captured: dict = {}

        async def fake_start_session(req: StartSessionRequest) -> str:
            captured["req"] = req
            return "sess_ctx"

        open_patch, close_patch = self._patch_client_lifecycle()
        with (
            open_patch,
            close_patch,
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.start_session",
                new=AsyncMock(side_effect=fake_start_session),
            ),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.seed_session", new=AsyncMock(return_value=None)
            ),
        ):
            middleware = _make_middleware(close_session=True)
            update = await middleware.abefore_agent({}, runtime)

        assert update == {"session_id": "sess_ctx"}
        req = captured["req"]
        assert isinstance(req, StartSessionRequest)
        assert req.base_image == "alpine:test"
        assert req.network_enabled is True
        assert req.memory_bytes == 1_234
        assert req.cpus == 2.5
        assert req.environment == {"X": "y"}

    async def test_check_command_policy_reads_from_ctx_sandbox(self, tmp_path: Path):
        """_check_command_policy must pull ``command_policy`` from ``ctx.sandbox``, not ``ctx.config.sandbox``."""
        from langchain.tools import ToolRuntime

        from automation.agent.middlewares.sandbox import _check_command_policy
        from codebase.context import SandboxRuntime
        from core.sandbox.command_policy import SandboxCommandPolicy

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        repo = Repo.init(repo_dir)

        # ctx.sandbox is the authoritative source; ctx.config.sandbox would not match.
        context = Mock(gitrepo=repo)
        context.config = Mock()
        context.config.sandbox = Mock()
        # Make config.sandbox.command_policy look like a default-empty policy to prove
        # the production code does NOT use it (the assertion below relies on a custom
        # policy that lives only on ctx.sandbox).
        context.config.sandbox.command_policy = Mock()
        context.config.sandbox.command_policy.disallow = ()
        context.config.sandbox.command_policy.allow = ()
        context.sandbox = SandboxRuntime(
            base_image="alpine:test",
            network_enabled=False,
            memory_bytes=None,
            cpus=None,
            env_vars={},
            command_policy=SandboxCommandPolicy(disallow=("custom-forbidden",)),
        )

        runtime = ToolRuntime(
            state={"session_id": "sess_1"},
            context=context,
            config={},
            stream_writer=Mock(),
            tool_call_id="call_policy_ctx",
            store=None,
        )

        with (
            patch.object(core_settings, "SANDBOX_COMMAND_POLICY_DISALLOW", ()),
            patch.object(core_settings, "SANDBOX_COMMAND_POLICY_ALLOW", ()),
        ):
            result = _check_command_policy("custom-forbidden --really", runtime)

        assert result is not None
        assert result.startswith("error:")
        assert "repo_disallow" in result


def test_sandbox_prompt_uses_workspace_paths():
    """The bash + scratchpad prompt blocks point at the /workspace layout (not /repo, /scratch)."""
    from automation.agent.middlewares.sandbox import BASH_TOOL_DESCRIPTION, SANDBOX_SYSTEM_PROMPT

    assert "/workspace/tmp" in SANDBOX_SYSTEM_PROMPT
    assert "/scratch" not in SANDBOX_SYSTEM_PROMPT
    assert "/workspace/repo" in BASH_TOOL_DESCRIPTION
    assert "/repos/" not in BASH_TOOL_DESCRIPTION


class TestWarmSessionHelpers:
    async def test_reuse_returns_none_without_thread_id(self):
        from automation.agent.middlewares.sandbox import SandboxMiddleware

        middleware = SandboxMiddleware(backend=Mock(), agent_root="/d")
        client = Mock()
        client.session_exists = AsyncMock(return_value=True)
        assert await middleware._reuse_warm_session(client, None) is None
        client.session_exists.assert_not_awaited()

    async def test_reuse_returns_session_when_alive_and_refreshes_ttl(self):
        from automation.agent.middlewares.sandbox import SANDBOX_SESSION_TTL_SECONDS, SandboxMiddleware

        middleware = SandboxMiddleware(backend=Mock(), agent_root="/d")
        client = Mock()
        client.session_exists = AsyncMock(return_value=True)

        fake_cache = Mock()
        fake_cache.aget = AsyncMock(return_value={"session_id": "warm-1"})
        fake_cache.aset = AsyncMock(return_value=None)
        fake_cache.adelete = AsyncMock(return_value=None)

        with patch("automation.agent.middlewares.sandbox.cache", fake_cache):
            result = await middleware._reuse_warm_session(client, "thread-1")

        assert result == "warm-1"
        client.session_exists.assert_awaited_once_with("warm-1")
        fake_cache.aset.assert_awaited_once_with(
            "sandbox_session:thread-1", {"session_id": "warm-1"}, timeout=SANDBOX_SESSION_TTL_SECONDS
        )

    async def test_reuse_drops_mapping_when_session_gone(self):
        from automation.agent.middlewares.sandbox import SandboxMiddleware

        middleware = SandboxMiddleware(backend=Mock(), agent_root="/d")
        client = Mock()
        client.session_exists = AsyncMock(return_value=False)

        fake_cache = Mock()
        fake_cache.aget = AsyncMock(return_value={"session_id": "stale-1"})
        fake_cache.aset = AsyncMock(return_value=None)
        fake_cache.adelete = AsyncMock(return_value=None)

        with patch("automation.agent.middlewares.sandbox.cache", fake_cache):
            result = await middleware._reuse_warm_session(client, "thread-1")

        assert result is None
        fake_cache.adelete.assert_awaited_once_with("sandbox_session:thread-1")

    async def test_remember_writes_mapping(self):
        from automation.agent.middlewares.sandbox import SANDBOX_SESSION_TTL_SECONDS, SandboxMiddleware

        middleware = SandboxMiddleware(backend=Mock(), agent_root="/d")
        fake_cache = Mock()
        fake_cache.aset = AsyncMock(return_value=None)
        with patch("automation.agent.middlewares.sandbox.cache", fake_cache):
            await middleware._remember_warm_session("thread-1", "sess-9")
        fake_cache.aset.assert_awaited_once_with(
            "sandbox_session:thread-1", {"session_id": "sess-9"}, timeout=SANDBOX_SESSION_TTL_SECONDS
        )
