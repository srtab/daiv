import base64
import io
import tarfile
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

from automation.agent.middlewares.sandbox import _run_bash_commands
from core.sandbox.schemas import RunCommandsResponse

if TYPE_CHECKING:
    from pathlib import Path


class TestSandboxArchiveLayout:
    @patch("automation.agent.middlewares.sandbox.DAIVSandboxClient")
    async def test_archive_is_rootless_and_excludes_git(self, mock_sandbox_client_class, tmp_path: Path):
        repo_dir = tmp_path / "repoX"
        (repo_dir / "src").mkdir(parents=True)
        (repo_dir / "src" / "app.py").write_text("print('hi')\n")
        (repo_dir / ".gitignore").write_text("*.pyc\n")
        (repo_dir / "pyproject.toml").write_text("[project]\nname = 'repoX'\n")

        # Should be excluded entirely (and anything inside it too)
        (repo_dir / ".git").mkdir()
        (repo_dir / ".git" / "config").write_text("[core]\nrepositoryformatversion = 0\n")

        mock_sandbox_client = Mock()
        mock_sandbox_client.run_commands = AsyncMock(return_value=RunCommandsResponse(results=[], patch=None))
        mock_sandbox_client_class.return_value = mock_sandbox_client

        response = await _run_bash_commands(["echo ok"], repo_dir, "sess_1")
        assert response is not None

        mock_sandbox_client.run_commands.assert_awaited_once()
        _session_id, request = mock_sandbox_client.run_commands.call_args.args

        archive_bytes = base64.b64decode(request.archive)
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
            names = tar.getnames()

        # Rootless: no repoX/ prefix
        assert ".gitignore" in names
        assert "pyproject.toml" in names
        assert "src" in names
        assert "src/app.py" in names
        assert not any(n.startswith("repoX/") for n in names)

        # `.git` is included in the archive
        assert any(n == ".git" or n.startswith(".git/") for n in names)
