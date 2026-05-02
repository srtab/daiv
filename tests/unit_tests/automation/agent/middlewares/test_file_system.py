from __future__ import annotations

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
