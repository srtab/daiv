from unittest.mock import AsyncMock

import httpx
import pytest
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.protocol import FileDownloadResponse, FileUploadResponse
from plugins.runtime import ResolvedPlugin

from automation.agent.constants import WORKSPACE_PATH
from automation.agent.workspace.git_runners import SandboxGitRunner
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
    assert isinstance(workspace.backend, CompositeBackend)
    assert (workspace.backend.default, workspace.backend.routes) == (workspace.bash, {})
    assert workspace.backend.artifacts_root == WORKSPACE_PATH
    assert workspace.git._runner == SandboxGitRunner(workspace.bash)


def test_a_sandbox_workspace_is_unfenced_and_seeded_with_the_skills():
    workspace = SandboxWorkspace(SandboxSession(FakeSandboxClient.opened(), sandbox_spec()))

    assert workspace.fs_permissions is None
    assert workspace.provisions_skills is True
    assert workspace.skills_provisioning_errors is workspace.session.skills_seed_errors


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


async def test_authenticated_git_refuses_an_unacquired_session():
    session = SandboxSession(FakeSandboxClient.opened(), sandbox_spec())
    session.refresh_credential = AsyncMock()

    with pytest.raises(RuntimeError, match="before the sandbox session is acquired"):
        await SandboxWorkspace(session).authenticated_git()

    session.refresh_credential.assert_not_awaited()


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


def _plugin(tmp_path, name, files, commit="c1"):
    root = tmp_path / name
    for relpath, text in files.items():
        (root / relpath).parent.mkdir(parents=True, exist_ok=True)
        (root / relpath).write_text(text)
    return ResolvedPlugin(name=name, commit=commit, host_root=root, skills=(), agents=())


def _plugin_workspace(manifest: bytes | None):
    workspace = SandboxWorkspace(acquired_session(AsyncMock(), "sid"))
    workspace.bash = AsyncMock(spec=SandboxFileBackend)
    workspace.bash.adownload_files.return_value = [
        FileDownloadResponse(path="m", content=manifest)
        if manifest is not None
        else FileDownloadResponse(path="m", error="file_not_found")
    ]
    workspace.bash.aupload_files.side_effect = lambda files: [FileUploadResponse(path=p) for p, _ in files]
    return workspace


async def test_sandbox_provision_skips_when_manifest_matches(tmp_path):
    workspace = _plugin_workspace(b"a@c1\nb@c1")

    errors = await workspace.provision_plugins([
        _plugin(tmp_path, "b", {"x.md": "x"}),
        _plugin(tmp_path, "a", {"x.md": "x"}),
    ])

    assert errors == []
    workspace.bash.run_commands.assert_not_awaited()
    workspace.bash.aupload_files.assert_not_awaited()


async def test_sandbox_provision_replaces_on_user_change(tmp_path):
    workspace = _plugin_workspace(b"other@c9")
    plugin = _plugin(tmp_path, "acme", {"skills/s/SKILL.md": "s", "bin/run.sh": "#!/bin/sh"})
    (plugin.host_root / "bin/run.sh").chmod(0o755)

    errors = await workspace.provision_plugins([plugin])

    assert errors == []
    commands = [call.args[0] for call in workspace.bash.run_commands.await_args_list]
    assert commands[0] == ["rm -rf /workspace/plugins"]
    assert commands[1] == ["chmod +x /workspace/plugins/acme/bin/run.sh"]
    uploads = [path for call in workspace.bash.aupload_files.await_args_list for path, _ in call.args[0]]
    assert uploads == [
        "/workspace/plugins/acme/bin/run.sh",
        "/workspace/plugins/acme/skills/s/SKILL.md",
        "/workspace/plugins/.daiv-manifest",
    ]
    assert workspace.bash.aupload_files.await_args_list[-1].args[0][0][1] == b"acme@c1"


async def test_sandbox_provision_withholds_manifest_after_upload_error(tmp_path):
    workspace = _plugin_workspace(None)
    workspace.bash.aupload_files.side_effect = lambda files: [
        FileUploadResponse(path=p, error="boom") for p, _ in files
    ]

    errors = await workspace.provision_plugins([_plugin(tmp_path, "acme", {"a.md": "a"})])

    assert errors == ["Plugin file /workspace/plugins/acme/a.md could not be provisioned: boom"]
    assert workspace.bash.aupload_files.await_count == 1


async def test_sandbox_provision_noop_when_not_ready(tmp_path):
    workspace = SandboxWorkspace(SandboxSession(FakeSandboxClient.opened(), sandbox_spec()))
    workspace.bash = AsyncMock(spec=SandboxFileBackend)

    assert await workspace.provision_plugins([_plugin(tmp_path, "acme", {"a.md": "a"})]) == []

    assert workspace.bash.mock_calls == []
