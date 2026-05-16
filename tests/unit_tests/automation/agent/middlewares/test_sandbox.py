import base64
import io
import tarfile
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

from git import Repo

from automation.agent.middlewares.file_system import DAIVFilesystemBackend
from automation.agent.middlewares.sandbox import SANDBOX_SYSTEM_PROMPT, SandboxMiddleware, _run_bash_commands
from core.conf import settings as core_settings
from core.sandbox.client import DAIVSandboxClient
from core.sandbox.schemas import RunCommandsResponse

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
    from codebase.repo_config import SandboxCommandPolicy

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


class TestBashTool:
    async def test_bash_tool_applies_patch_and_returns_results_json(self, tmp_path: Path):
        repo_dir = tmp_path / "repoX"
        repo_dir.mkdir(parents=True)
        repo = Repo.init(repo_dir)

        # Configure git identity for commits
        with repo.config_writer() as writer:
            writer.set_value("user", "name", "Test User")
            writer.set_value("user", "email", "test@example.com")

        file_path = repo_dir / "hello.txt"
        file_path.write_text("old\n")
        repo.git.add("-A")
        repo.index.commit("init")

        # Create a real patch via git diff.
        file_path.write_text("new\n")
        patch_text = repo.git.diff("HEAD")
        if patch_text and not patch_text.endswith("\n"):
            patch_text += "\n"
        # Restore file so the patch application is observable.
        repo.git.checkout("--", "hello.txt")

        assert file_path.read_text() == "old\n"

        response = RunCommandsResponse(results=[], patch=base64.b64encode(patch_text.encode("utf-8")).decode("utf-8"))

        runtime = _make_bash_runtime(repo)
        client = Mock()
        client.run_commands = AsyncMock(return_value=response)
        middleware = SandboxMiddleware(
            backend=Mock(spec=DAIVFilesystemBackend), agent_root=f"/{repo_dir.name}", close_session=True
        )
        middleware._client = client
        bash_tool = middleware.tools[0]

        output = await bash_tool.coroutine(command="echo ok", runtime=runtime)

        assert file_path.read_text() == "new\n"
        import json as _json

        payload = _json.loads(output)
        assert payload["commands"] == []
        # The patch just edits hello.txt — nothing added/deleted/renamed.
        assert payload["files_changed"] == [{"path": "hello.txt", "op": "modified"}]
        client.run_commands.assert_awaited_once()

    async def test_bash_tool_returns_error_when_apply_patch_raises(self, tmp_path: Path):
        """If ``GitManager.apply_patch`` raises (e.g. malformed sandbox patch), the bash tool
        must surface a "Failed to persist" error string instead of returning the JSON
        success payload — otherwise the agent's local filesystem silently desyncs from
        the sandbox while bash claims success."""
        repo_dir = tmp_path / "repoX"
        repo_dir.mkdir(parents=True)
        repo = Repo.init(repo_dir)

        # Non-empty patch so the response.patch branch is taken.
        response = RunCommandsResponse(
            results=[], patch=base64.b64encode(b"diff --git a/x b/x\nbogus\n").decode("utf-8")
        )

        runtime = _make_bash_runtime(repo)
        client = Mock()
        client.run_commands = AsyncMock(return_value=response)
        middleware = SandboxMiddleware(
            backend=Mock(spec=DAIVFilesystemBackend), agent_root=f"/{repo_dir.name}", close_session=True
        )
        middleware._client = client
        bash_tool = middleware.tools[0]

        with patch(
            "automation.agent.middlewares.sandbox.GitManager.apply_patch",
            side_effect=RuntimeError("simulated apply failure"),
        ):
            output = await bash_tool.coroutine(command="echo ok", runtime=runtime)

        assert output.startswith("error: Failed to persist")

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
        run_mock = AsyncMock(return_value=RunCommandsResponse(results=[], patch=None))
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
        run_commands_mock = AsyncMock(return_value=RunCommandsResponse(results=[], patch=None))
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


class TestResolveRepoBackend:
    """Validate the helper that the patch-apply and gitignore-guard dispatches rely on:
    with composite present, the underlying repo backend is unwrapped (so isinstance
    dispatch works); without composite, the backend passes through.
    """

    def test_composite_returns_underlying_default(self, tmp_path: Path):
        from automation.agent.middlewares.file_system import DAIVCompositeBackend, DAIVFilesystemBackend
        from automation.agent.middlewares.sandbox import _resolve_repo_backend

        skills = DAIVFilesystemBackend(root_dir=tmp_path / "s", virtual_mode=True)
        (tmp_path / "s").mkdir()
        repo = DAIVFilesystemBackend(root_dir=tmp_path / "r", virtual_mode=True)
        (tmp_path / "r").mkdir()
        composite = DAIVCompositeBackend(default=repo, routes={"/skills/": skills})

        assert _resolve_repo_backend(composite, "/myrepo") is repo

    def test_bare_backend_passes_through(self, tmp_path: Path):
        from automation.agent.middlewares.file_system import DAIVFilesystemBackend
        from automation.agent.middlewares.sandbox import _resolve_repo_backend

        backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        assert _resolve_repo_backend(backend, "/myrepo") is backend


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

        open_patch, close_patch = self._patch_client_lifecycle()
        with (
            open_patch,
            close_patch,
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.start_session",
                new=AsyncMock(return_value="sess_1"),
            ) as start_session_mock,
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.seed_session", new=AsyncMock(return_value=None)
            ) as seed_session_mock,
        ):
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

        skills_dir = tmp_path / "skills"
        (skills_dir / "skill-one").mkdir(parents=True)
        (skills_dir / "skill-one" / "SKILL.md").write_text("hi")

        runtime = _make_agent_runtime(repo_working_dir=str(repo_dir))

        open_patch, close_patch = self._patch_client_lifecycle()
        with (
            open_patch,
            close_patch,
            patch("automation.agent.middlewares.sandbox.SKILLS_CACHE_PATH", skills_dir),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.start_session",
                new=AsyncMock(return_value="sess_skills"),
            ),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.seed_session", new=AsyncMock(return_value=None)
            ) as seed_session_mock,
        ):
            middleware = SandboxMiddleware(backend=Mock(), agent_root=f"/{repo_dir.name}", close_session=True)
            update = await middleware.abefore_agent({}, runtime)

        assert update == {"session_id": "sess_skills"}
        seed_session_mock.assert_awaited_once()
        _args, kwargs = seed_session_mock.call_args
        assert isinstance(kwargs.get("repo_archive"), (bytes, bytearray))
        assert isinstance(kwargs.get("skills_archive"), (bytes, bytearray))

    def test_make_skills_archive_returns_none_when_dir_missing(self, tmp_path: Path):
        from automation.agent.middlewares.sandbox import _make_skills_archive

        assert _make_skills_archive(tmp_path / "nonexistent") is None

    def test_make_skills_archive_returns_none_when_dir_empty(self, tmp_path: Path):
        from automation.agent.middlewares.sandbox import _make_skills_archive

        empty = tmp_path / "skills"
        empty.mkdir()
        assert _make_skills_archive(empty) is None

    def test_make_skills_archive_packs_children_relative_to_root(self, tmp_path: Path):
        from automation.agent.middlewares.sandbox import _make_skills_archive

        skills = tmp_path / "skills"
        (skills / "skill-one").mkdir(parents=True)
        (skills / "skill-one" / "SKILL.md").write_text("hello")
        (skills / "skill-two").mkdir()
        (skills / "skill-two" / "SKILL.md").write_text("world")

        archive = _make_skills_archive(skills)
        assert isinstance(archive, bytes)

        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
            names = sorted(tf.getnames())
        assert "skill-one/SKILL.md" in names
        assert "skill-two/SKILL.md" in names
        # No top-level wrapper directory.
        assert not any(n.startswith("skills/") for n in names)

    def test_make_skills_archive_returns_none_on_iterdir_oserror(self, tmp_path: Path):
        from automation.agent.middlewares.sandbox import _make_skills_archive

        skills = tmp_path / "skills"
        skills.mkdir()

        with patch("automation.agent.middlewares.sandbox.Path.iterdir", side_effect=PermissionError("denied")):
            result = _make_skills_archive(skills)

        assert result is None

    def test_make_skills_archive_returns_none_on_tar_error(self, tmp_path: Path):
        from automation.agent.middlewares.sandbox import _make_skills_archive

        skills = tmp_path / "skills"
        (skills / "skill-one").mkdir(parents=True)
        (skills / "skill-one" / "SKILL.md").write_text("hello")

        with patch("automation.agent.middlewares.sandbox.tarfile.open", side_effect=tarfile.TarError("boom")):
            result = _make_skills_archive(skills)

        assert result is None

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
        from codebase.repo_config import SandboxCommandPolicy
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
        from codebase.repo_config import SandboxCommandPolicy

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


class TestAwrapToolCall:
    """Tool dispatch is intercepted at ToolNode level, not via ``awrap_model_call``.

    These tests pin the entry point: ``awrap_tool_call`` must intercept dispatched
    write_file/edit_file calls so that the mirror runs against ``ToolNode.tools_by_name``,
    which is built once at agent-creation time and not updated by ``awrap_model_call``.
    """

    @staticmethod
    def _request(tool_name: str, args: dict, *, runtime=None):
        from langgraph.prebuilt.tool_node import ToolCallRequest

        tool = Mock(spec_set=["name"])
        tool.name = tool_name
        return ToolCallRequest(
            tool_call={"name": tool_name, "args": args, "id": "call_1", "type": "tool_call"},
            tool=tool,
            state={},
            runtime=runtime or Mock(),
        )

    async def test_passes_through_when_tool_is_not_write_or_edit(self, tmp_path: Path):
        request = self._request("read_file", {"file_path": "/repo/foo.py"})
        middleware = _make_middleware()
        middleware._syncer = Mock()  # set so we don't bypass on syncer-missing path

        sentinel = object()

        async def handler(req):
            assert req is request
            return sentinel

        result = await middleware.awrap_tool_call(request, handler)
        assert result is sentinel

    async def test_passes_through_when_syncer_not_initialized(self, tmp_path: Path):
        """Pre-``abefore_agent`` dispatch is rare but must not raise."""
        request = self._request("write_file", {"file_path": "/repo/foo.py", "content": "x"})
        middleware = _make_middleware()
        # _syncer is None — abefore_agent never ran.

        sentinel = object()

        async def handler(req):
            return sentinel

        result = await middleware.awrap_tool_call(request, handler)
        assert result is sentinel

    async def test_passes_through_when_tool_is_none(self, tmp_path: Path):
        """Unregistered tool calls reach awrap_tool_call with ``request.tool=None``."""
        from langgraph.prebuilt.tool_node import ToolCallRequest

        request = ToolCallRequest(
            tool_call={"name": "unknown", "args": {}, "id": "call_1", "type": "tool_call"},
            tool=None,
            state={},
            runtime=Mock(),
        )
        middleware = _make_middleware()
        middleware._syncer = Mock()

        sentinel = object()

        async def handler(req):
            return sentinel

        result = await middleware.awrap_tool_call(request, handler)
        assert result is sentinel

    async def test_write_file_refused_outside_agent_root(self, tmp_path: Path):
        """A write to ``/skills/...`` (or any path outside agent_root) must be refused
        before dispatch. The skills mount is shared across concurrent agent runs in this
        process; an errant write here would later trigger a rollback ``backend.delete``
        that clobbers a file other runs depend on.
        """
        from langchain_core.messages import ToolMessage

        from automation.agent.middlewares.file_system import SandboxSyncer

        backend = Mock(spec=DAIVFilesystemBackend)
        runtime = Mock()
        runtime.context = Mock(has_repo=True)
        runtime.state = {"session_id": "sess_1"}

        middleware = _make_middleware()
        middleware._backend = backend
        middleware._agent_root = "/myrepo"
        middleware._syncer = SandboxSyncer(backend=backend, agent_root="/myrepo", client=Mock())

        request = self._request(
            "write_file", {"file_path": "/skills/some-skill/SKILL.md", "content": "x"}, runtime=runtime
        )
        handler = AsyncMock()

        result = await middleware.awrap_tool_call(request, handler)

        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert "Refused" in result.content
        assert "/myrepo/" in result.content
        handler.assert_not_called()
        backend.delete.assert_not_called()

    async def test_write_file_dispatches_when_resolve_target_fails(self, tmp_path: Path):
        """Pre-dispatch path-resolution failure must fall through to upstream, not
        synthesize a refusal — upstream owns the canonical "invalid path" error."""
        from langchain_core.messages import ToolMessage

        from automation.agent.middlewares.file_system import SandboxSyncer

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        repo = Repo.init(repo_dir)

        backend = Mock(spec=DAIVFilesystemBackend)
        backend._resolve_path = Mock(side_effect=ValueError("traversal"))

        runtime = Mock()
        runtime.context = Mock(gitrepo=repo, has_repo=True)
        runtime.state = {"session_id": "sess_1"}

        middleware = _make_middleware()
        middleware._backend = backend
        middleware._agent_root = f"/{repo_dir.name}"
        middleware._syncer = SandboxSyncer(backend=backend, agent_root=f"/{repo_dir.name}", client=Mock())

        request = self._request("write_file", {"file_path": f"/{repo_dir.name}/foo", "content": "x"}, runtime=runtime)
        sentinel = ToolMessage(content="upstream error", tool_call_id="call_1", name="write_file", status="error")
        handler = AsyncMock(return_value=sentinel)

        result = await middleware.awrap_tool_call(request, handler)

        handler.assert_called_once()
        assert result is sentinel

    async def test_write_file_refused_when_path_is_gitignored(self, tmp_path: Path):
        """write_file on a `.gitignore`-matching path must be refused before the upstream
        handler runs — `git add -A` would silently drop the file, so the agent would
        report success while the change never reached the merge request."""
        from langchain_core.messages import ToolMessage

        from automation.agent.middlewares.file_system import SandboxSyncer

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        repo = Repo.init(repo_dir)
        (repo_dir / ".gitignore").write_text(".python-version\n")

        target = repo_dir / ".python-version"

        backend = Mock(spec=DAIVFilesystemBackend)
        backend._to_path = Mock(return_value=target)

        runtime = Mock()
        runtime.context = Mock(gitrepo=repo, has_repo=True)
        runtime.state = {"session_id": "sess_1"}

        middleware = _make_middleware()
        middleware._backend = backend
        middleware._agent_root = f"/{repo_dir.name}"
        middleware._syncer = SandboxSyncer(backend=backend, agent_root=f"/{repo_dir.name}", client=Mock())

        request = self._request(
            "write_file", {"file_path": f"/{repo_dir.name}/.python-version", "content": "3.11\n"}, runtime=runtime
        )
        handler = AsyncMock()

        result = await middleware.awrap_tool_call(request, handler)

        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert ".gitignore" in result.content
        assert not target.exists()
        handler.assert_not_called()

    async def test_write_file_refused_when_gitignore_check_unknown(self, tmp_path: Path):
        """If `git check-ignore` itself fails (corrupt repo, missing binary, permissions),
        the helper returns ``IgnoreCheck.UNKNOWN`` and the write must be refused — failing
        open here would silently re-introduce the `git add -A` drop the guard prevents."""
        from langchain_core.messages import ToolMessage

        from automation.agent.middlewares.file_system import SandboxSyncer
        from codebase.utils import GitManager, IgnoreCheck

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        repo = Repo.init(repo_dir)
        target = repo_dir / "foo.py"

        backend = Mock(spec=DAIVFilesystemBackend)
        backend._to_path = Mock(return_value=target)

        runtime = Mock()
        runtime.context = Mock(gitrepo=repo, has_repo=True)
        runtime.state = {"session_id": "sess_1"}

        middleware = _make_middleware()
        middleware._backend = backend
        middleware._agent_root = f"/{repo_dir.name}"
        middleware._syncer = SandboxSyncer(backend=backend, agent_root=f"/{repo_dir.name}", client=Mock())

        request = self._request(
            "write_file", {"file_path": f"/{repo_dir.name}/foo.py", "content": "x\n"}, runtime=runtime
        )
        handler = AsyncMock()

        with patch.object(GitManager, "is_path_ignored", return_value=IgnoreCheck.UNKNOWN):
            result = await middleware.awrap_tool_call(request, handler)

        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert "could not determine" in result.content
        handler.assert_not_called()
