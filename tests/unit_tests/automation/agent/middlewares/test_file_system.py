from __future__ import annotations

import os
import stat
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from deepagents.middleware.filesystem import FilesystemMiddleware as UpstreamFilesystemMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from automation.agent.middlewares import file_system as fs_module
from automation.agent.middlewares.file_system import (
    EDIT_SUCCESS_PREFIX,
    WRITE_SUCCESS_PREFIX,
    DAIVFilesystemBackend,
    DAIVStoreBackend,
)
from automation.agent.middlewares.sandbox import SandboxMiddleware
from core.sandbox.schemas import ApplyMutationsResponse, MutationResult

if TYPE_CHECKING:
    from pathlib import Path

    from langchain_core.tools import BaseTool


pytestmark = pytest.mark.usefixtures("bypass_gitignore_check")


@pytest.fixture
def working_repo(tmp_path) -> Path:
    """Test layout: tmp_path/myrepo/."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    return repo


@pytest.fixture
def fake_client() -> AsyncMock:
    return AsyncMock()


@pytest.fixture(autouse=True)
def _sandbox_api_key(monkeypatch):
    """``SandboxMiddleware.__init__`` refuses to instantiate without an API key."""
    from core.site_settings import site_settings

    monkeypatch.setattr(site_settings, "sandbox_api_key", Mock(get_secret_value=lambda: "test-key"))


@pytest.fixture
def setup(working_repo, fake_client):
    """Backend + upstream tool map + sandbox middleware with pre-installed client/syncer."""
    backend = DAIVFilesystemBackend(root_dir=working_repo.parent, virtual_mode=True)
    fs = UpstreamFilesystemMiddleware(backend=backend, custom_tool_descriptions=fs_module.CUSTOM_TOOL_DESCRIPTIONS)
    tools = {tool.name: tool for tool in fs.tools}
    agent_root = f"/{working_repo.name}"
    middleware = SandboxMiddleware(backend=backend, agent_root=agent_root, working_dir=working_repo)
    middleware._client = fake_client
    middleware._syncer = fs_module.SandboxSyncer(backend=backend, agent_root=agent_root, client=fake_client)
    return SimpleNamespace(backend=backend, tools=tools, middleware=middleware, repo=working_repo)


def _runtime(*, state: dict[str, Any], working_dir: Path) -> SimpleNamespace:
    """A minimal ``ToolRuntime`` shim sufficient for the syncer + upstream tools."""
    return SimpleNamespace(
        state=state,
        context=SimpleNamespace(gitrepo=SimpleNamespace(working_dir=str(working_dir)), has_repo=True),
        tool_call_id="call_test",
    )


async def _invoke(middleware: SandboxMiddleware, tool: BaseTool, args: dict, runtime: SimpleNamespace):
    """Run ``args`` through ``middleware.awrap_tool_call``, dispatching to ``tool``."""
    request = ToolCallRequest(
        tool_call={"name": tool.name, "args": args, "id": runtime.tool_call_id, "type": "tool_call"},
        tool=tool,
        state=runtime.state,
        runtime=runtime,
    )

    async def handler(req: ToolCallRequest) -> ToolMessage:
        # Mimic ToolNode._execute_tool_async: invoke the coroutine with runtime injected,
        # and normalize a string return into a ToolMessage. ToolMessage returns (e.g. permission
        # denied) pass through unchanged.
        result = await req.tool.coroutine(**req.tool_call["args"], runtime=req.runtime)
        if isinstance(result, ToolMessage):
            return result
        return ToolMessage(content=result, tool_call_id=req.tool_call["id"], name=req.tool.name)

    return await middleware.awrap_tool_call(request, handler)


def _content(result) -> str:
    assert isinstance(result, ToolMessage), f"expected ToolMessage, got {type(result)}"
    assert isinstance(result.content, str)
    return result.content


def _mirror_ok(path: str = "/repo/foo.py") -> ApplyMutationsResponse:
    return ApplyMutationsResponse(results=[MutationResult(path=path, ok=True, error=None)])


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


async def test_write_file_writes_locally_and_mirrors(setup, fake_client):
    fake_client.apply_file_mutations.return_value = _mirror_ok()
    runtime = _runtime(state={"session_id": "sid"}, working_dir=setup.repo)

    result = await _invoke(
        setup.middleware,
        setup.tools["write_file"],
        {"file_path": f"/{setup.repo.name}/foo.py", "content": "print('hi')\n"},
        runtime,
    )

    assert (setup.repo / "foo.py").read_text() == "print('hi')\n"
    fake_client.apply_file_mutations.assert_awaited_once()
    mutation = fake_client.apply_file_mutations.call_args.args[1].mutations[0]
    assert mutation.path == "/repo/foo.py"
    assert mutation.content == b"print('hi')\n"
    assert mutation.mode == 0o644
    assert WRITE_SUCCESS_PREFIX in _content(result)


@pytest.mark.parametrize(
    ("apply_kwargs", "expected_substring"),
    [
        ({"side_effect": RuntimeError("network down")}, "network down"),
        (
            {
                "return_value": ApplyMutationsResponse(
                    results=[MutationResult(path="/repo/foo.py", ok=False, error="server-side validation failed")]
                )
            },
            "server-side validation failed",
        ),
        (
            {
                "side_effect": httpx.HTTPStatusError(
                    "500", request=httpx.Request("POST", "x"), response=httpx.Response(500)
                )
            },
            "sandbox sync raised",
        ),
    ],
    ids=["mirror_exception", "per_item_failure", "http_5xx"],
)
async def test_write_file_rolls_back_on_mirror_failure(setup, fake_client, apply_kwargs, expected_substring):
    for attr, value in apply_kwargs.items():
        setattr(fake_client.apply_file_mutations, attr, value)
    runtime = _runtime(state={"session_id": "sid"}, working_dir=setup.repo)

    result = await _invoke(
        setup.middleware,
        setup.tools["write_file"],
        {"file_path": f"/{setup.repo.name}/foo.py", "content": "x"},
        runtime,
    )

    assert not (setup.repo / "foo.py").exists()
    text = _content(result)
    assert text.startswith("Error:"), f"rollback succeeded → expected Error: prefix, got {text!r}"
    assert expected_substring in text


async def test_write_file_missing_session_id_rolls_back(setup, fake_client):
    runtime = _runtime(state={}, working_dir=setup.repo)

    result = await _invoke(
        setup.middleware,
        setup.tools["write_file"],
        {"file_path": f"/{setup.repo.name}/foo.py", "content": "x"},
        runtime,
    )

    assert not (setup.repo / "foo.py").exists()
    assert "session" in _content(result).lower()
    fake_client.apply_file_mutations.assert_not_awaited()


async def test_write_file_rejects_path_outside_working_dir(working_repo, fake_client, tmp_path):
    """A write to a path outside the agent root is rolled back; sandbox is never called."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    write = next(
        t
        for t in UpstreamFilesystemMiddleware(
            backend=backend, custom_tool_descriptions=fs_module.CUSTOM_TOOL_DESCRIPTIONS
        ).tools
        if t.name == "write_file"
    )
    agent_root = f"/{working_repo.name}"
    middleware = SandboxMiddleware(backend=backend, agent_root=agent_root, working_dir=working_repo)
    middleware._client = fake_client
    middleware._syncer = fs_module.SandboxSyncer(backend=backend, agent_root=agent_root, client=fake_client)

    result = await _invoke(
        middleware,
        write,
        {"file_path": f"/{outside.name}/leak.py", "content": "oops"},
        _runtime(state={"session_id": "sid"}, working_dir=working_repo),
    )

    assert not (outside / "leak.py").exists()
    fake_client.apply_file_mutations.assert_not_awaited()
    assert "Error" in _content(result)


async def test_write_file_surfaces_critical_when_rollback_fails(setup, fake_client, monkeypatch):
    """Sandbox sync fails AND rollback also fails → CRITICAL marker so the agent sees the desync."""
    fake_client.apply_file_mutations.side_effect = RuntimeError("sandbox down")
    runtime = _runtime(state={"session_id": "sid"}, working_dir=setup.repo)

    real_unlink = os.unlink

    def fail_unlink(path, *a, **kw):  # noqa: ARG001
        if str(path).endswith("foo.py"):
            raise OSError("disk gone")
        return real_unlink(path, *a, **kw)

    monkeypatch.setattr(os, "unlink", fail_unlink)

    result = await _invoke(
        setup.middleware,
        setup.tools["write_file"],
        {"file_path": f"/{setup.repo.name}/foo.py", "content": "x"},
        runtime,
    )

    text = _content(result)
    assert "CRITICAL" in text
    assert "rollback also failed" in text


async def test_write_file_skips_mirror_on_upstream_error(setup, fake_client):
    """Upstream rejects writes to existing files; sandbox is never called."""
    (setup.repo / "foo.py").write_text("already-here\n")
    runtime = _runtime(state={"session_id": "sid"}, working_dir=setup.repo)

    result = await _invoke(
        setup.middleware,
        setup.tools["write_file"],
        {"file_path": f"/{setup.repo.name}/foo.py", "content": "new"},
        runtime,
    )

    fake_client.apply_file_mutations.assert_not_awaited()
    assert "already exists" in _content(result)
    assert (setup.repo / "foo.py").read_text() == "already-here\n"


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------


async def test_edit_file_writes_locally_and_mirrors(setup, fake_client):
    target = setup.repo / "foo.py"
    target.write_text("hello world\n")
    os.chmod(target, 0o755)  # noqa: PTH101, S103
    fake_client.apply_file_mutations.return_value = _mirror_ok()
    runtime = _runtime(state={"session_id": "sid"}, working_dir=setup.repo)

    result = await _invoke(
        setup.middleware,
        setup.tools["edit_file"],
        {"file_path": f"/{setup.repo.name}/foo.py", "old_string": "hello", "new_string": "goodbye"},
        runtime,
    )

    assert target.read_text() == "goodbye world\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    mutation = fake_client.apply_file_mutations.call_args.args[1].mutations[0]
    assert mutation.mode == 0o755
    assert mutation.content == b"goodbye world\n"
    assert EDIT_SUCCESS_PREFIX in _content(result)


@pytest.mark.parametrize(
    "side_effect",
    [RuntimeError("network down"), httpx.RequestError("conn refused", request=httpx.Request("POST", "x"))],
    ids=["mirror_exception", "network_error"],
)
async def test_edit_file_rolls_back_on_mirror_failure(setup, fake_client, side_effect):
    target = setup.repo / "foo.py"
    target.write_text("original\n")
    os.chmod(target, 0o644)  # noqa: PTH101
    fake_client.apply_file_mutations.side_effect = side_effect
    runtime = _runtime(state={"session_id": "sid"}, working_dir=setup.repo)

    result = await _invoke(
        setup.middleware,
        setup.tools["edit_file"],
        {"file_path": f"/{setup.repo.name}/foo.py", "old_string": "original", "new_string": "modified"},
        runtime,
    )

    assert target.read_text() == "original\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    text = _content(result)
    assert "Error" in text or "failed" in text.lower()


async def test_edit_file_surfaces_critical_when_rollback_fails(setup, fake_client, monkeypatch):
    """Sync fails AND restoring pre-edit bytes also fails → CRITICAL marker so the agent sees the desync."""
    target = setup.repo / "foo.py"
    target.write_text("original\n")
    fake_client.apply_file_mutations.side_effect = RuntimeError("sandbox down")
    runtime = _runtime(state={"session_id": "sid"}, working_dir=setup.repo)

    # Rollback restores pre-edit content via ``backend.aupload_files``; raise from inside
    # it to simulate a disk-level failure during recovery.
    async def fail_upload(_files):
        raise OSError("disk gone")

    monkeypatch.setattr(setup.backend, "aupload_files", fail_upload)

    result = await _invoke(
        setup.middleware,
        setup.tools["edit_file"],
        {"file_path": f"/{setup.repo.name}/foo.py", "old_string": "original", "new_string": "modified"},
        runtime,
    )

    text = _content(result)
    assert "CRITICAL" in text
    assert "rollback also failed" in text


async def test_edit_file_pre_read_failure_defers_to_upstream(setup, fake_client):
    """``edit_file`` on a missing path defers to upstream's "not found" error; sandbox is never called."""
    runtime = _runtime(state={"session_id": "sid"}, working_dir=setup.repo)

    result = await _invoke(
        setup.middleware,
        setup.tools["edit_file"],
        {"file_path": f"/{setup.repo.name}/missing.py", "old_string": "a", "new_string": "b"},
        runtime,
    )

    assert "not found" in _content(result).lower()
    assert not (setup.repo / "missing.py").exists()
    fake_client.apply_file_mutations.assert_not_awaited()


# ---------------------------------------------------------------------------
# Bypass conditions
# ---------------------------------------------------------------------------


async def test_non_write_tool_passes_through(setup, fake_client):
    (setup.repo / "foo.py").write_text("hi\n")
    runtime = _runtime(state={"session_id": "sid"}, working_dir=setup.repo)

    result = await _invoke(
        setup.middleware, setup.tools["read_file"], {"file_path": f"/{setup.repo.name}/foo.py"}, runtime
    )

    assert "hi" in _content(result)
    fake_client.apply_file_mutations.assert_not_awaited()


async def test_no_syncer_falls_back_to_handler(setup, fake_client):
    """Pre-``abefore_agent`` dispatch (no syncer) is not intercepted; the handler runs as-is."""
    setup.middleware._client = None
    setup.middleware._syncer = None
    runtime = _runtime(state={"session_id": "sid"}, working_dir=setup.repo)

    result = await _invoke(
        setup.middleware,
        setup.tools["write_file"],
        {"file_path": f"/{setup.repo.name}/foo.py", "content": "x"},
        runtime,
    )

    assert (setup.repo / "foo.py").exists()
    fake_client.apply_file_mutations.assert_not_awaited()
    assert WRITE_SUCCESS_PREFIX in _content(result)


# ---------------------------------------------------------------------------
# Upstream contract pinning
# ---------------------------------------------------------------------------


async def test_upstream_success_prefixes_remain_stable(setup):
    """A deepagents bump that rewords either prefix would silently disable sandbox sync.

    Pinning the constants here makes the bump fail this test instead of failing in production.
    """
    runtime = _runtime(state={}, working_dir=setup.repo)

    write_result = await setup.tools["write_file"].coroutine(
        file_path=f"/{setup.repo.name}/contract.py", content="x", runtime=runtime
    )
    assert write_result.startswith(WRITE_SUCCESS_PREFIX), (
        f"upstream changed write success format; update WRITE_SUCCESS_PREFIX: {write_result!r}"
    )

    edit_result = await setup.tools["edit_file"].coroutine(
        file_path=f"/{setup.repo.name}/contract.py", old_string="x", new_string="y", runtime=runtime
    )
    assert edit_result.startswith(EDIT_SUCCESS_PREFIX), (
        f"upstream changed edit success format; update EDIT_SUCCESS_PREFIX: {edit_result!r}"
    )


@pytest.fixture
def store_setup(fake_client):
    """Same shape as ``setup`` but backed by a ``StoreBackend`` (no on-disk working tree)."""
    from langgraph.store.memory import InMemoryStore

    store = InMemoryStore()
    backend = DAIVStoreBackend(store=store, namespace=lambda _rt: ("daiv-repoless-fs", "test-thread"))
    fs = UpstreamFilesystemMiddleware(backend=backend, custom_tool_descriptions=fs_module.CUSTOM_TOOL_DESCRIPTIONS)
    tools = {tool.name: tool for tool in fs.tools}
    middleware = SandboxMiddleware(backend=backend, agent_root="/repo", working_dir=None)
    middleware._client = fake_client
    middleware._syncer = fs_module.SandboxSyncer(backend=backend, agent_root="/repo", client=fake_client)
    return SimpleNamespace(backend=backend, store=store, tools=tools, middleware=middleware)


class _RepolessCtx:
    """Repoless ctx shim — ``has_repo`` False, ``gitrepo`` access raises."""

    has_repo = False

    @property
    def gitrepo(self):  # pragma: no cover — defense against accidental repo-bound code paths
        raise AssertionError("repoless test accessed gitrepo")


def _repoless_runtime(*, state: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(state=state, context=_RepolessCtx(), tool_call_id="call_repoless")


async def test_repoless_write_file_lands_in_store(store_setup, fake_client):
    fake_client.apply_file_mutations.return_value = _mirror_ok()
    runtime = _repoless_runtime(state={"session_id": "sid"})

    result = await _invoke(
        store_setup.middleware,
        store_setup.tools["write_file"],
        {"file_path": "/repo/note.md", "content": "hello\n"},
        runtime,
    )

    assert WRITE_SUCCESS_PREFIX in _content(result)
    item = await store_setup.store.aget(("daiv-repoless-fs", "test-thread"), "/repo/note.md")
    assert item is not None
    assert item.value["content"] == "hello\n"
    fake_client.apply_file_mutations.assert_awaited_once()
    assert fake_client.apply_file_mutations.call_args.args[1].mutations[0].path == "/repo/note.md"


async def test_repoless_write_file_rolls_back_via_store_delete(store_setup, fake_client):
    """Sandbox sync failure → store.adelete restores the pre-write state (file absent)."""
    fake_client.apply_file_mutations.side_effect = RuntimeError("network down")
    runtime = _repoless_runtime(state={"session_id": "sid"})

    result = await _invoke(
        store_setup.middleware, store_setup.tools["write_file"], {"file_path": "/repo/lost.md", "content": "x"}, runtime
    )

    text = _content(result)
    assert text.startswith("Error:"), f"rollback succeeded → expected Error: prefix, got {text!r}"
    item = await store_setup.store.aget(("daiv-repoless-fs", "test-thread"), "/repo/lost.md")
    assert item is None, "write rollback must remove the just-created store entry"


async def test_repoless_write_file_surfaces_critical_when_store_delete_fails(store_setup, fake_client, monkeypatch):
    """If both sandbox sync AND ``DAIVStoreBackend.delete`` fail during a repoless write
    rollback, the agent must see the CRITICAL marker so it doesn't silently proceed against
    a desynced store/sandbox pair."""
    fake_client.apply_file_mutations.side_effect = RuntimeError("sandbox down")
    runtime = _repoless_runtime(state={"session_id": "sid"})

    async def fail_delete(_path):
        return False

    monkeypatch.setattr(store_setup.backend, "delete", fail_delete)

    result = await _invoke(
        store_setup.middleware,
        store_setup.tools["write_file"],
        {"file_path": "/repo/ghost.md", "content": "x"},
        runtime,
    )

    text = _content(result)
    assert "CRITICAL" in text
    assert "rollback also failed" in text


async def test_repoless_edit_file_rolls_back_via_aupload_files(store_setup, fake_client):
    """Sandbox sync failure on an edit must restore pre-edit content via the backend."""
    fake_client.apply_file_mutations.return_value = _mirror_ok()
    runtime = _repoless_runtime(state={"session_id": "sid"})

    seed = await _invoke(
        store_setup.middleware,
        store_setup.tools["write_file"],
        {"file_path": "/repo/doc.md", "content": "before\n"},
        runtime,
    )
    assert WRITE_SUCCESS_PREFIX in _content(seed)

    fake_client.apply_file_mutations.reset_mock(side_effect=True)
    fake_client.apply_file_mutations.side_effect = RuntimeError("sandbox down")

    edit = await _invoke(
        store_setup.middleware,
        store_setup.tools["edit_file"],
        {"file_path": "/repo/doc.md", "old_string": "before", "new_string": "after"},
        runtime,
    )

    text = _content(edit)
    assert text.startswith("Error:"), f"rollback succeeded → expected Error: prefix, got {text!r}"
    item = await store_setup.store.aget(("daiv-repoless-fs", "test-thread"), "/repo/doc.md")
    assert item is not None and item.value["content"] == "before\n", "edit rollback must restore pre-edit content"
