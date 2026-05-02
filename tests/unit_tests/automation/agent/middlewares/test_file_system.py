from __future__ import annotations

import os
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _make_runtime(state, working_dir, gitrepo=None):
    return SimpleNamespace(
        state=state, context=SimpleNamespace(gitrepo=gitrepo or SimpleNamespace(working_dir=str(working_dir)))
    )


@pytest.fixture
def fake_client(monkeypatch):
    """Patch DAIVSandboxClient with an AsyncMock and yield the mock."""
    from automation.agent.middlewares import file_system as fs_module

    mock = AsyncMock()
    monkeypatch.setattr(fs_module, "DAIVSandboxClient", lambda: mock)
    return mock


@pytest.fixture
def working_repo(tmp_path):
    """A working_dir layout: tmp_path/repo/."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    return repo


async def test_sync_write_file_writes_locally_and_syncs(fake_client, working_repo):
    from deepagents.backends.filesystem import FilesystemBackend

    from automation.agent.middlewares.file_system import FilesystemMiddleware
    from core.sandbox.schemas import ApplyMutationsResponse, MutationResult

    fake_client.apply_file_mutations.return_value = ApplyMutationsResponse(
        results=[MutationResult(path="/repo/foo.py", ok=True, error=None)]
    )
    backend = FilesystemBackend(root_dir=working_repo.parent, virtual_mode=True)
    mw = FilesystemMiddleware(backend=backend, sandbox_sync=True, working_dir=working_repo)

    write = next(t for t in mw.tools if t.name == "write_file")
    runtime = _make_runtime(state={"session_id": "sid"}, working_dir=working_repo)

    result = await write.coroutine(file_path=f"/{working_repo.name}/foo.py", content="print('hi')\n", runtime=runtime)

    # Local write happened.
    assert (working_repo / "foo.py").read_text() == "print('hi')\n"
    # Sync called with the right path + content + mode.
    fake_client.apply_file_mutations.assert_awaited_once()
    args = fake_client.apply_file_mutations.call_args
    request = args.args[1]
    mutation = request.mutations[0]
    assert mutation.path == "/repo/foo.py"
    assert mutation.content == b"print('hi')\n"
    assert mutation.mode == 0o644
    assert "Updated file" in result


async def test_sync_write_file_rolls_back_on_sync_failure(fake_client, working_repo):
    from deepagents.backends.filesystem import FilesystemBackend

    from automation.agent.middlewares.file_system import FilesystemMiddleware

    fake_client.apply_file_mutations.side_effect = RuntimeError("network down")
    backend = FilesystemBackend(root_dir=working_repo.parent, virtual_mode=True)
    mw = FilesystemMiddleware(backend=backend, sandbox_sync=True, working_dir=working_repo)

    write = next(t for t in mw.tools if t.name == "write_file")
    runtime = _make_runtime(state={"session_id": "sid"}, working_dir=working_repo)

    result = await write.coroutine(file_path=f"/{working_repo.name}/foo.py", content="print('hi')\n", runtime=runtime)

    # File deleted (rollback).
    assert not (working_repo / "foo.py").exists()
    assert "Error" in result or "failed" in result.lower()


async def test_sync_write_file_rolls_back_on_per_item_failure(fake_client, working_repo):
    from deepagents.backends.filesystem import FilesystemBackend

    from automation.agent.middlewares.file_system import FilesystemMiddleware
    from core.sandbox.schemas import ApplyMutationsResponse, MutationResult

    fake_client.apply_file_mutations.return_value = ApplyMutationsResponse(
        results=[MutationResult(path="/repo/foo.py", ok=False, error="server-side validation failed")]
    )
    backend = FilesystemBackend(root_dir=working_repo.parent, virtual_mode=True)
    mw = FilesystemMiddleware(backend=backend, sandbox_sync=True, working_dir=working_repo)

    write = next(t for t in mw.tools if t.name == "write_file")
    runtime = _make_runtime(state={"session_id": "sid"}, working_dir=working_repo)

    result = await write.coroutine(file_path=f"/{working_repo.name}/foo.py", content="x", runtime=runtime)

    assert not (working_repo / "foo.py").exists()
    assert "server-side validation failed" in result


async def test_sync_write_file_missing_session_id(fake_client, working_repo):
    from deepagents.backends.filesystem import FilesystemBackend

    from automation.agent.middlewares.file_system import FilesystemMiddleware

    backend = FilesystemBackend(root_dir=working_repo.parent, virtual_mode=True)
    mw = FilesystemMiddleware(backend=backend, sandbox_sync=True, working_dir=working_repo)

    write = next(t for t in mw.tools if t.name == "write_file")
    runtime = _make_runtime(state={}, working_dir=working_repo)  # no session_id

    result = await write.coroutine(file_path=f"/{working_repo.name}/foo.py", content="x", runtime=runtime)
    assert not (working_repo / "foo.py").exists()
    assert "session" in result.lower()
    fake_client.apply_file_mutations.assert_not_awaited()


async def test_sync_edit_file_writes_locally_and_syncs(fake_client, working_repo):
    from deepagents.backends.filesystem import FilesystemBackend

    from automation.agent.middlewares.file_system import FilesystemMiddleware
    from core.sandbox.schemas import ApplyMutationsResponse, MutationResult

    target = working_repo / "foo.py"
    target.write_text("hello world\n")
    os.chmod(target, 0o755)  # noqa: PTH101, S103

    fake_client.apply_file_mutations.return_value = ApplyMutationsResponse(
        results=[MutationResult(path="/repo/foo.py", ok=True, error=None)]
    )
    backend = FilesystemBackend(root_dir=working_repo.parent, virtual_mode=True)
    mw = FilesystemMiddleware(backend=backend, sandbox_sync=True, working_dir=working_repo)

    edit = next(t for t in mw.tools if t.name == "edit_file")
    runtime = _make_runtime(state={"session_id": "sid"}, working_dir=working_repo)

    result = await edit.coroutine(
        file_path=f"/{working_repo.name}/foo.py", old_string="hello", new_string="goodbye", runtime=runtime
    )

    assert (working_repo / "foo.py").read_text() == "goodbye world\n"
    # Mode preserved.
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    args = fake_client.apply_file_mutations.call_args
    mutation = args.args[1].mutations[0]
    assert mutation.mode == 0o755
    assert mutation.content == b"goodbye world\n"
    assert "Successfully replaced" in result


async def test_sync_edit_file_rolls_back_on_sync_failure(fake_client, working_repo):
    from deepagents.backends.filesystem import FilesystemBackend

    from automation.agent.middlewares.file_system import FilesystemMiddleware

    target = working_repo / "foo.py"
    target.write_text("original\n")
    os.chmod(target, 0o644)  # noqa: PTH101

    fake_client.apply_file_mutations.side_effect = RuntimeError("network down")
    backend = FilesystemBackend(root_dir=working_repo.parent, virtual_mode=True)
    mw = FilesystemMiddleware(backend=backend, sandbox_sync=True, working_dir=working_repo)

    edit = next(t for t in mw.tools if t.name == "edit_file")
    runtime = _make_runtime(state={"session_id": "sid"}, working_dir=working_repo)

    result = await edit.coroutine(
        file_path=f"/{working_repo.name}/foo.py", old_string="original", new_string="modified", runtime=runtime
    )

    # Pre-edit content and mode restored.
    assert (working_repo / "foo.py").read_text() == "original\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert "Error" in result or "failed" in result.lower()


async def test_write_file_handles_5xx_with_rollback(fake_client, working_repo):
    """A 500 from the sandbox propagates as a tool error and triggers rollback."""
    import httpx
    from deepagents.backends.filesystem import FilesystemBackend

    from automation.agent.middlewares.file_system import FilesystemMiddleware

    fake_client.apply_file_mutations.side_effect = httpx.HTTPStatusError(
        "500", request=httpx.Request("POST", "x"), response=httpx.Response(500)
    )
    backend = FilesystemBackend(root_dir=working_repo.parent, virtual_mode=True)
    mw = FilesystemMiddleware(backend=backend, sandbox_sync=True, working_dir=working_repo)

    write = next(t for t in mw.tools if t.name == "write_file")
    runtime = _make_runtime(state={"session_id": "sid"}, working_dir=working_repo)

    result = await write.coroutine(file_path=f"/{working_repo.name}/foo.py", content="x", runtime=runtime)
    assert not (working_repo / "foo.py").exists()
    assert "Error" in result


async def test_edit_file_handles_network_error(fake_client, working_repo):
    """A network error during edit_file rolls back to the pre-edit content."""
    import httpx
    from deepagents.backends.filesystem import FilesystemBackend

    from automation.agent.middlewares.file_system import FilesystemMiddleware

    target = working_repo / "foo.py"
    target.write_text("a\nb\n")
    fake_client.apply_file_mutations.side_effect = httpx.RequestError(
        "conn refused", request=httpx.Request("POST", "x")
    )

    backend = FilesystemBackend(root_dir=working_repo.parent, virtual_mode=True)
    mw = FilesystemMiddleware(backend=backend, sandbox_sync=True, working_dir=working_repo)
    edit = next(t for t in mw.tools if t.name == "edit_file")
    runtime = _make_runtime(state={"session_id": "sid"}, working_dir=working_repo)

    result = await edit.coroutine(
        file_path=f"/{working_repo.name}/foo.py", old_string="a", new_string="z", runtime=runtime
    )
    assert (working_repo / "foo.py").read_text() == "a\nb\n"
    assert "Error" in result


async def test_sandbox_sync_disabled_uses_unmodified_tools(working_repo):
    """When sandbox_sync=False, deepagents' tools run without sync."""
    from deepagents.backends.filesystem import FilesystemBackend

    from automation.agent.middlewares.file_system import FilesystemMiddleware

    backend = FilesystemBackend(root_dir=working_repo.parent, virtual_mode=True)
    mw = FilesystemMiddleware(backend=backend, sandbox_sync=False)

    write = next(t for t in mw.tools if t.name == "write_file")
    runtime = _make_runtime(state={}, working_dir=working_repo)

    result = await write.coroutine(file_path=f"/{working_repo.name}/foo.py", content="x", runtime=runtime)

    assert (working_repo / "foo.py").exists()
    assert "Updated file" in result


async def test_sync_write_file_rejects_path_outside_working_dir(fake_client, working_repo, tmp_path):
    """A write to a path outside working_dir is rolled back; sandbox is never called."""
    from deepagents.backends.filesystem import FilesystemBackend

    from automation.agent.middlewares.file_system import FilesystemMiddleware

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    mw = FilesystemMiddleware(backend=backend, sandbox_sync=True, working_dir=working_repo)

    write = next(t for t in mw.tools if t.name == "write_file")
    runtime = _make_runtime(state={"session_id": "sid"}, working_dir=working_repo)

    result = await write.coroutine(file_path=f"/{outside.name}/leak.py", content="oops", runtime=runtime)

    assert not (outside / "leak.py").exists()
    fake_client.apply_file_mutations.assert_not_awaited()
    assert "Error" in result


async def test_sync_edit_file_pre_read_failure(fake_client, working_repo):
    """edit_file on a missing path returns a read error, never calls the sandbox, never creates the file."""
    from deepagents.backends.filesystem import FilesystemBackend

    from automation.agent.middlewares.file_system import FilesystemMiddleware

    backend = FilesystemBackend(root_dir=working_repo.parent, virtual_mode=True)
    mw = FilesystemMiddleware(backend=backend, sandbox_sync=True, working_dir=working_repo)

    edit = next(t for t in mw.tools if t.name == "edit_file")
    runtime = _make_runtime(state={"session_id": "sid"}, working_dir=working_repo)

    result = await edit.coroutine(
        file_path=f"/{working_repo.name}/missing.py", old_string="a", new_string="b", runtime=runtime
    )

    assert "cannot read" in result.lower()
    assert not (working_repo / "missing.py").exists()
    fake_client.apply_file_mutations.assert_not_awaited()


async def test_sync_write_surfaces_critical_when_rollback_fails(fake_client, working_repo, monkeypatch):
    """If the sandbox sync fails AND rollback also fails, the agent gets a CRITICAL marker."""
    from deepagents.backends.filesystem import FilesystemBackend

    from automation.agent.middlewares.file_system import FilesystemMiddleware

    fake_client.apply_file_mutations.side_effect = RuntimeError("sandbox down")
    backend = FilesystemBackend(root_dir=working_repo.parent, virtual_mode=True)
    mw = FilesystemMiddleware(backend=backend, sandbox_sync=True, working_dir=working_repo)

    write = next(t for t in mw.tools if t.name == "write_file")
    runtime = _make_runtime(state={"session_id": "sid"}, working_dir=working_repo)

    real_unlink = os.unlink

    def fail_unlink(path, *a, **kw):  # noqa: ARG001
        if str(path).endswith("foo.py"):
            raise OSError("disk gone")
        return real_unlink(path, *a, **kw)

    monkeypatch.setattr(os, "unlink", fail_unlink)

    result = await write.coroutine(file_path=f"/{working_repo.name}/foo.py", content="x", runtime=runtime)

    assert "CRITICAL" in result
    assert "rollback also failed" in result
