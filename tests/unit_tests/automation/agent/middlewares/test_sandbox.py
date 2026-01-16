import base64
import io
import tarfile
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

from git import Repo
from pydantic import SecretStr

from automation.agent.middlewares.sandbox import SANDBOX_SYSTEM_PROMPT, SandboxMiddleware, _run_bash_commands, bash_tool
from core.conf import settings as core_settings
from core.sandbox.schemas import RunCommandsResponse

if TYPE_CHECKING:
    from pathlib import Path


def _make_agent_runtime(*, repo_working_dir: str) -> Mock:
    runtime = Mock()
    runtime.context = Mock()
    runtime.context.repo = Mock()
    runtime.context.repo.working_dir = repo_working_dir
    runtime.context.config = Mock()
    runtime.context.config.sandbox = Mock()
    runtime.context.config.sandbox.base_image = "python:3.12"
    return runtime


class TestBashTool:
    async def test_bash_tool_applies_patch_and_returns_results_json(self, tmp_path: Path):
        from langchain.tools import ToolRuntime

        repo_dir = tmp_path / "repoX"
        repo_dir.mkdir(parents=True)
        repo = Repo.init(repo_dir)

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

        runtime = ToolRuntime(
            state={"session_id": "sess_1"},
            context=Mock(repo=repo),
            config={},
            stream_writer=Mock(),
            tool_call_id=None,
            store=None,
        )

        with patch("automation.agent.middlewares.sandbox._run_bash_commands", new=AsyncMock(return_value=response)):
            output = await bash_tool.coroutine(command="echo ok", runtime=runtime)  # type: ignore[union-attr]

        assert file_path.read_text() == "new\n"
        assert output == "[]"

    async def test_bash_tool_returns_error_when_sandbox_call_fails(self, tmp_path: Path):
        from langchain.tools import ToolRuntime

        repo_dir = tmp_path / "repoX"
        repo_dir.mkdir(parents=True)
        repo = Repo.init(repo_dir)

        runtime = ToolRuntime(
            state={"session_id": "sess_1"},
            context=Mock(repo=repo),
            config={},
            stream_writer=Mock(),
            tool_call_id=None,
            store=None,
        )

        with patch("automation.agent.middlewares.sandbox._run_bash_commands", new=AsyncMock(return_value=None)):
            output = await bash_tool.coroutine(command="echo ok", runtime=runtime)  # type: ignore[union-attr]

        assert output.startswith("error: Failed to run command.")


class TestRunBashCommands:
    async def test_run_bash_commands_creates_archive_and_includes_git(self, tmp_path: Path):
        repo_dir = tmp_path / "repoX"
        (repo_dir / "src").mkdir(parents=True)
        (repo_dir / "src" / "app.py").write_text("print('hi')\n")
        (repo_dir / ".gitignore").write_text("*.pyc\n")
        (repo_dir / "pyproject.toml").write_text("[project]\nname = 'repoX'\n")

        # Note: current implementation includes `.git` in the archive.
        (repo_dir / ".git").mkdir()
        (repo_dir / ".git" / "config").write_text("[core]\nrepositoryformatversion = 0\n")

        run_commands_mock = AsyncMock(return_value=RunCommandsResponse(results=[], patch=None))
        with patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.run_commands", new=run_commands_mock):
            response = await _run_bash_commands(["echo ok"], repo_dir, "sess_1")

        assert response is not None

        run_commands_mock.assert_awaited_once()
        _session_id, request = run_commands_mock.call_args.args

        archive_bytes = base64.b64decode(request.archive)
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
            names = tar.getnames()

        # Rootless: no repoX/ prefix
        assert ".gitignore" in names
        assert "pyproject.toml" in names
        assert "src" in names
        assert "src/app.py" in names
        assert not any(n.startswith("repoX/") for n in names)

        # `.git` is included in the archive (current behavior)
        assert any(n == ".git" or n.startswith(".git/") for n in names)


class TestSandboxMiddleware:
    async def test_abefore_agent_starts_session_and_sets_session_id(self, tmp_path: Path):
        runtime = _make_agent_runtime(repo_working_dir=str(tmp_path / "repoX"))

        with (
            patch.object(core_settings, "SANDBOX_API_KEY", SecretStr("test")),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.start_session",
                new=AsyncMock(return_value="sess_1"),
            ) as start_session_mock,
        ):
            middleware = SandboxMiddleware(close_session=True)
            update = await middleware.abefore_agent({}, runtime)

        assert update == {"session_id": "sess_1"}
        start_session_mock.assert_awaited_once()

    async def test_abefore_agent_reuses_session_id_when_close_session_false(self, tmp_path: Path):
        runtime = _make_agent_runtime(repo_working_dir=str(tmp_path / "repoX"))
        state = {"session_id": "sess_existing"}

        with (
            patch.object(core_settings, "SANDBOX_API_KEY", SecretStr("test")),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.start_session",
                new=AsyncMock(return_value="sess_1"),
            ) as start_session_mock,
        ):
            middleware = SandboxMiddleware(close_session=False)
            update = await middleware.abefore_agent(state, runtime)

        assert update is None
        start_session_mock.assert_not_awaited()

    async def test_aafter_agent_closes_session_and_clears_session_id(self, tmp_path: Path):
        runtime = _make_agent_runtime(repo_working_dir=str(tmp_path / "repoX"))
        state = {"session_id": "sess_1"}

        with (
            patch.object(core_settings, "SANDBOX_API_KEY", SecretStr("test")),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.close_session", new=AsyncMock(return_value=None)
            ) as close_session_mock,
        ):
            middleware = SandboxMiddleware(close_session=True)
            update = await middleware.aafter_agent(state, runtime)

        assert update == {"session_id": None}
        close_session_mock.assert_awaited_once_with("sess_1")

    async def test_aafter_agent_does_not_close_session_when_close_session_false(self, tmp_path: Path):
        runtime = _make_agent_runtime(repo_working_dir=str(tmp_path / "repoX"))
        state = {"session_id": "sess_1"}

        with (
            patch.object(core_settings, "SANDBOX_API_KEY", SecretStr("test")),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.close_session", new=AsyncMock(return_value=None)
            ) as close_session_mock,
        ):
            middleware = SandboxMiddleware(close_session=False)
            update = await middleware.aafter_agent(state, runtime)

        assert update is None
        close_session_mock.assert_not_awaited()

    async def test_awrap_model_call_appends_sandbox_system_prompt(self, tmp_path: Path):
        from langchain.agents.middleware import ModelRequest, ModelResponse

        runtime = _make_agent_runtime(repo_working_dir=str(tmp_path / "repoX"))

        with patch.object(core_settings, "SANDBOX_API_KEY", SecretStr("test")):
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
