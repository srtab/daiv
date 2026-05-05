import base64
import io
import tarfile
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

from git import Repo

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


def _make_agent_runtime(*, repo_working_dir: str) -> Mock:
    runtime = Mock()
    runtime.context = Mock()
    runtime.context.gitrepo = Mock(working_dir=repo_working_dir)
    runtime.context.config = _make_sandbox_config_mock()
    return runtime


def _make_bash_runtime(repo: Repo, disallow=(), allow=()) -> Mock:
    """Build a ToolRuntime-compatible mock for bash_tool tests."""
    from langchain.tools import ToolRuntime

    runtime = ToolRuntime(
        state={"session_id": "sess_1"},
        context=Mock(gitrepo=repo, config=_make_sandbox_config_mock(disallow=disallow, allow=allow)),
        config={},
        stream_writer=Mock(),
        tool_call_id="call_1",
        store=None,
    )
    return runtime


def _bash_tool_with_fake_client(client: Mock):
    """Build a fresh SandboxMiddleware with ``client`` pre-installed and return its bash tool."""
    middleware = SandboxMiddleware(close_session=True)
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
        bash_tool = _bash_tool_with_fake_client(client)

        output = await bash_tool.coroutine(command="echo ok", runtime=runtime)

        assert file_path.read_text() == "new\n"
        import json as _json

        payload = _json.loads(output)
        assert payload["commands"] == []
        # The patch just edits hello.txt — nothing added/deleted/renamed.
        assert payload["files_changed"] == [{"path": "hello.txt", "op": "modified"}]
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
        middleware = SandboxMiddleware(close_session=True)
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
            middleware = SandboxMiddleware(close_session=True)
            update = await middleware.abefore_agent({}, runtime)

        assert update == {"session_id": "sess_1"}
        start_session_mock.assert_awaited_once()
        seed_session_mock.assert_awaited_once()
        # seed_session is called with (session_id, repo_archive=...) — assert the keyword.
        _args, kwargs = seed_session_mock.call_args
        assert "repo_archive" in kwargs
        assert isinstance(kwargs["repo_archive"], (bytes, bytearray))
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
            middleware = SandboxMiddleware(close_session=True)
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
            middleware = SandboxMiddleware(close_session=True)
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
            middleware = SandboxMiddleware(close_session=True)
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
            middleware = SandboxMiddleware(close_session=False)
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
            middleware = SandboxMiddleware(close_session=True)
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
            middleware = SandboxMiddleware(close_session=False)
            middleware._client = DAIVSandboxClient()
            update = await middleware.aafter_agent(state, runtime)

        assert update is None
        close_session_mock.assert_not_awaited()
        # Subagent path: session is the parent's, but the client we opened still must be released.
        client_close_mock.assert_awaited_once()
        assert middleware._client is None

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

    async def test_awrap_model_call_appends_sandbox_system_prompt(self, tmp_path: Path):
        from langchain.agents.middleware import ModelRequest, ModelResponse

        runtime = _make_agent_runtime(repo_working_dir=str(tmp_path / "repoX"))

        middleware = SandboxMiddleware()

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
