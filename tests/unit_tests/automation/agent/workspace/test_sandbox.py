import hashlib
import shutil
import subprocess  # noqa: S404
from unittest.mock import AsyncMock

import httpx
import pytest
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.protocol import FileUploadResponse
from skills.constants import PLUGINS_PATH

from automation.agent.constants import WORKSPACE_PATH
from automation.agent.workspace.git_runners import SandboxGitRunner
from automation.agent.workspace.sandbox import (
    PLUGINS_FINGERPRINT_COMMAND,
    PluginFile,
    SandboxWorkspace,
    _fingerprint,
    _plugin_files,
)
from automation.agent.workspace.sandbox_backend import DOWNLOAD_TOO_LARGE, SandboxFileBackend
from automation.agent.workspace.session import SandboxSession
from core.sandbox.schemas import RunCommandResult, RunCommandsResponse
from tests.unit_tests.conftest import FakeSandboxClient, acquired_session, sandbox_spec
from tests.unit_tests.plugins.helpers import resolved_plugin


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


def _result(output="", exit_code=0):
    return RunCommandsResponse(results=[RunCommandResult(command="c", output=output, exit_code=exit_code)])


class _PluginSandbox:
    """The container side of plugin provisioning: its ``/workspace/plugins`` files, the commands run on them and the
    uploads, with commands starting with a ``failing`` prefix exiting 1."""

    def __init__(self, files=None, failing=()):
        self.files = dict(files or {})
        self.failing = failing
        self.commands = []
        self.uploads = []

    async def run_commands(self, commands, *, fail_fast):
        (command,) = commands
        self.commands.append(command)
        if self.failing and command.startswith(self.failing):
            return _result(exit_code=1)
        if command == PLUGINS_FINGERPRINT_COMMAND:
            listed = [
                PluginFile(None, path, hashlib.sha256(data).hexdigest(), False) for path, data in self.files.items()
            ]
            return _result(f"{_fingerprint(listed)}  -\n")
        if command.startswith("rm -rf "):
            self.files.clear()
        return _result()

    async def aupload_files(self, files):
        self.uploads.extend(path for path, _ in files)
        self.files.update(files)
        return [FileUploadResponse(path=path) for path, _ in files]


def _plugin_workspace(sandbox):
    workspace = SandboxWorkspace(acquired_session(AsyncMock(), "sid"))
    workspace.bash = sandbox
    return workspace


async def test_sandbox_provision_skips_when_the_files_already_match(tmp_path):
    sandbox = _PluginSandbox({"/workspace/plugins/a/x.md": b"x", "/workspace/plugins/b/y.md": b"y"})

    errors = await _plugin_workspace(sandbox).provision_plugins([
        resolved_plugin(tmp_path, "b", {"y.md": "y"}),
        resolved_plugin(tmp_path, "a", {"x.md": "x"}),
    ])

    assert errors == []
    assert sandbox.commands == [PLUGINS_FINGERPRINT_COMMAND]
    assert sandbox.uploads == []


async def test_sandbox_provision_replaces_another_users_plugins(tmp_path):
    sandbox = _PluginSandbox({"/workspace/plugins/other/x.md": b"x"})
    plugin = resolved_plugin(tmp_path, "acme", {"skills/s/SKILL.md": "s", "bin/run.sh": "#!/bin/sh"})
    (plugin.host_root / "bin/run.sh").chmod(0o755)

    errors = await _plugin_workspace(sandbox).provision_plugins([plugin])

    assert errors == []
    assert sandbox.commands == [
        PLUGINS_FINGERPRINT_COMMAND,
        "rm -rf /workspace/plugins",
        "chmod +x /workspace/plugins/acme/bin/run.sh",
    ]
    assert sandbox.files == {
        "/workspace/plugins/acme/bin/run.sh": b"#!/bin/sh",
        "/workspace/plugins/acme/skills/s/SKILL.md": b"s",
    }


async def test_sandbox_provision_restores_a_file_changed_in_an_earlier_turn(tmp_path):
    sandbox = _PluginSandbox({"/workspace/plugins/acme/SKILL.md": b"injected"})

    errors = await _plugin_workspace(sandbox).provision_plugins([
        resolved_plugin(tmp_path, "acme", {"SKILL.md": "real"})
    ])

    assert errors == []
    assert sandbox.files == {"/workspace/plugins/acme/SKILL.md": b"real"}


async def test_sandbox_provision_clears_the_previous_users_plugins_for_a_turn_without_any():
    sandbox = _PluginSandbox({"/workspace/plugins/acme/x.md": b"x"})

    assert await _plugin_workspace(sandbox).provision_plugins([]) == []

    assert sandbox.commands == [PLUGINS_FINGERPRINT_COMMAND, "rm -rf /workspace/plugins"]
    assert (sandbox.files, sandbox.uploads) == ({}, [])


async def test_sandbox_provision_without_plugins_on_a_clean_sandbox_only_checks():
    sandbox = _PluginSandbox()

    assert await _plugin_workspace(sandbox).provision_plugins([]) == []

    assert sandbox.commands == [PLUGINS_FINGERPRINT_COMMAND]


async def test_sandbox_provision_reprovisions_when_the_fingerprint_fails(tmp_path):
    sandbox = _PluginSandbox({"/workspace/plugins/acme/a.md": b"a"}, failing=("find ",))

    errors = await _plugin_workspace(sandbox).provision_plugins([resolved_plugin(tmp_path, "acme", {"a.md": "a"})])

    assert errors == []
    assert sandbox.uploads == ["/workspace/plugins/acme/a.md"]


async def test_sandbox_provision_reports_upload_errors(tmp_path):
    sandbox = _PluginSandbox()

    async def failing_upload(files):
        return [FileUploadResponse(path=path, error="boom") for path, _ in files]

    sandbox.aupload_files = failing_upload

    errors = await _plugin_workspace(sandbox).provision_plugins([resolved_plugin(tmp_path, "acme", {"a.md": "a"})])

    assert errors == ["Plugin file /workspace/plugins/acme/a.md could not be provisioned: boom"]


async def test_sandbox_provision_noop_when_not_ready(tmp_path):
    workspace = SandboxWorkspace(SandboxSession(FakeSandboxClient.opened(), sandbox_spec()))
    workspace.bash = AsyncMock(spec=SandboxFileBackend)

    assert await workspace.provision_plugins([resolved_plugin(tmp_path, "acme", {"a.md": "a"})]) == []

    assert workspace.bash.mock_calls == []


@pytest.mark.parametrize("failing", ["rm ", "chmod "])
async def test_sandbox_provision_reports_a_failed_command(tmp_path, failing):
    sandbox = _PluginSandbox({"/workspace/plugins/old/x.md": b"x"}, failing=(failing,))
    plugin = resolved_plugin(tmp_path, "acme", {"run.sh": "#!/bin/sh"})
    (plugin.host_root / "run.sh").chmod(0o755)

    errors = await _plugin_workspace(sandbox).provision_plugins([plugin])

    assert len(errors) == 1
    assert errors[0].startswith("The plugins could not be provisioned")


def test_fingerprint_matches_what_the_container_command_prints(tmp_path):
    plugin = resolved_plugin(tmp_path / "host", "acme", {"b.md": "same", "a.md": "same", "sub/c.sh": "#!/bin/sh"})
    mounted = tmp_path / "workspace" / "plugins"
    shutil.copytree(plugin.host_root, mounted / "acme")

    command = PLUGINS_FINGERPRINT_COMMAND.replace(PLUGINS_PATH, str(mounted))
    printed = subprocess.run(["sh", "-c", command], capture_output=True, text=True, check=True).stdout  # noqa: S603, S607

    assert printed.split()[0] == _fingerprint(_plugin_files(plugin.host_root, f"{mounted}/acme"))
