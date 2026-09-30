from unittest.mock import AsyncMock

import httpx
import pytest

from automation.agent.constants import WORKSPACE_PATH
from automation.agent.git_runners import SandboxGitRunner
from automation.agent.middlewares.file_system import READ_ONLY_PERMISSIONS, DAIVCompositeBackend
from automation.agent.workspace.sandbox import SandboxWorkspace
from automation.agent.workspace.sandbox_backend import DOWNLOAD_TOO_LARGE, SandboxFileBackend
from automation.agent.workspace.session import SandboxSession
from core.sandbox.schemas import RunCommandResult, RunCommandsResponse
from tests.unit_tests.conftest import FakeSandboxClient, acquired_session, sandbox_spec


def test_the_files_the_shell_and_git_all_reach_the_one_session():
    """B6's premise: one ``SandboxFileBackend`` over the run's session backs the file tools, the ``bash`` tool and git,
    so the agent, its subagents and the publisher all work in the same container."""
    session = SandboxSession(FakeSandboxClient.opened(), sandbox_spec())
    workspace = SandboxWorkspace(session)

    assert workspace.session is session
    assert isinstance(workspace.bash, SandboxFileBackend)
    assert workspace.bash.session is session
    assert isinstance(workspace.backend, DAIVCompositeBackend)
    assert (workspace.backend.default, workspace.backend.routes) == (workspace.bash, {})
    assert workspace.backend.artifacts_root == WORKSPACE_PATH
    assert workspace.git._runner == SandboxGitRunner(workspace.bash)


def test_a_sandbox_workspace_is_unfenced_and_seeded_with_the_skills():
    workspace = SandboxWorkspace(SandboxSession(FakeSandboxClient.opened(), sandbox_spec()))

    assert workspace.fs_permissions is None
    assert workspace.explore_permissions is READ_ONLY_PERMISSIONS
    assert workspace.provisions_skills is True


async def test_it_is_ready_once_its_session_is_acquired():
    client = FakeSandboxClient.opened()
    session_id = client.add_running_session("sess-1")
    session = SandboxSession(client, sandbox_spec())
    workspace = SandboxWorkspace(session)

    assert workspace.is_ready is False
    await session.acquire(prior_id=session_id, prior_fingerprint=None, seed=AsyncMock())
    assert workspace.is_ready is True


async def test_authenticated_git_refreshes_the_credential_before_handing_back_git():
    """B8: the publish's network git runs with a token re-minted onto the container first."""
    session = acquired_session(FakeSandboxClient.opened())
    session.refresh_credential = AsyncMock(return_value=True)
    workspace = SandboxWorkspace(session)

    assert await workspace.authenticated_git() is workspace.git
    session.refresh_credential.assert_awaited_once_with()


@pytest.mark.parametrize(
    "error", [RuntimeError("mint failed"), httpx.ConnectError("down")], ids=["failed-mint", "failed-delivery"]
)
async def test_a_failed_refresh_still_hands_back_git(error, caplog):
    """B8: a failed re-mint or delivery is exception-logged, and the publish goes on with the turn-start token."""
    session = acquired_session(FakeSandboxClient.opened())
    session.refresh_credential = AsyncMock(side_effect=error)
    workspace = SandboxWorkspace(session)

    with caplog.at_level("ERROR", logger="daiv.tools"):
        assert await workspace.authenticated_git() is workspace.git

    assert "Could not refresh the sandbox egress token" in caplog.text


async def test_a_sandbox_download_is_capped_inside_the_container():
    """An oversized file is refused in the sandbox, so it never crosses the wire."""
    client = AsyncMock()
    client.run_commands.return_value = RunCommandsResponse(
        results=[RunCommandResult(command="download", output="", exit_code=6)]
    )
    workspace = SandboxWorkspace(acquired_session(client, "sid"))

    response = await workspace.download_file("/workspace/tmp/huge.log", max_bytes=1234)

    (command,) = client.run_commands.call_args.args[1].commands
    assert "-gt 1234 ]" in command
    assert response.error == DOWNLOAD_TOO_LARGE
