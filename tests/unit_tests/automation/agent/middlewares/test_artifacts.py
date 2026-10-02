from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from deepagents.backends.protocol import FILE_NOT_FOUND, FileDownloadResponse
from langchain.tools import ToolRuntime

from automation.agent.artifacts import PUBLISH_ARTIFACT_TOOL_NAME, ArtifactError
from automation.agent.middlewares.artifacts import ARTIFACTS_SYSTEM_PROMPT, ArtifactsMiddleware, _workspace_path_error
from automation.agent.workspace.sandbox_backend import DOWNLOAD_TOO_LARGE
from tests.unit_tests.conftest import FakeArtifactStore, FakeWorkspace

THREAD_ID = "thread-1"
NO_SESSION = (
    "Error publishing artifact: this run has no session to attach artifacts to. "
    "Put the key content in your final response instead."
)
PRE_STORE_DESCRIPTION_SHA256 = "f9609027009d3f805be7bbf061b989f5b2ef0832f21d0db87a14477e5f405bab"


class _FakeBackend:
    """A ``/workspace`` backend that serves an in-memory file map through ``adownload_files``."""

    def __init__(self, files: dict[str, bytes] | None = None, *, error: str | None = None):
        self.files = files or {}
        self.error = error
        self.requested: list[str] = []

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        self.requested.extend(paths)
        out = []
        for path in paths:
            if self.error:
                out.append(FileDownloadResponse(path=path, content=None, error=self.error))
            elif path in self.files:
                out.append(FileDownloadResponse(path=path, content=self.files[path], error=None))
            else:
                out.append(FileDownloadResponse(path=path, content=None, error=FILE_NOT_FOUND))
        return out


def _runtime(thread_id: str | None) -> ToolRuntime:
    configurable = {"thread_id": thread_id} if thread_id else {}
    return ToolRuntime(
        state={},
        context=Mock(),
        config={"configurable": configurable},
        stream_writer=Mock(),
        tool_call_id="c1",
        store=None,
    )


def _tool(workspace, store: FakeArtifactStore | None = None):
    """The tool's coroutine over ``workspace`` and ``store`` (an accepting fake unless given)."""
    (tool,) = ArtifactsMiddleware(workspace=workspace, store=store or FakeArtifactStore()).tools
    assert tool.name == PUBLISH_ARTIFACT_TOOL_NAME
    return tool.coroutine


def test_tool_call_schema_hides_runtime_and_makes_title_optional():
    tool = ArtifactsMiddleware(workspace=FakeWorkspace(), store=FakeArtifactStore()).tools[0]
    schema = tool.tool_call_schema.model_json_schema()
    assert set(schema["properties"]) == {"path", "title"}
    assert schema["required"] == ["path"]


def test_description_quotes_the_store_limits():
    store = FakeArtifactStore(max_bytes=1234, per_run_max=7)
    tool = ArtifactsMiddleware(workspace=FakeWorkspace(), store=store).tools[0]
    assert "Limits: 1234 bytes per file, 7 files per run." in tool.description


def test_description_at_the_default_limits_is_the_one_production_shipped():
    """The tools array is part of every cached prompt, so at the default limits the text must stay byte-identical to
    the one shipped before the store existed."""
    store = FakeArtifactStore(max_bytes=10 * 1024 * 1024, per_run_max=20)
    tool = ArtifactsMiddleware(workspace=FakeWorkspace(), store=store).tools[0]
    assert hashlib.sha256(tool.description.encode()).hexdigest() == PRE_STORE_DESCRIPTION_SHA256


@pytest.mark.parametrize(
    ("path", "fragment"),
    [
        ("relative/report.md", "must be an absolute path"),
        ("/workspace/../etc/passwd", "no '..' segments"),
        ("/workspace", "outside /workspace"),
        ("/workspacefoo/report.md", "outside /workspace"),
        ("/etc/passwd", "outside /workspace"),
    ],
)
def test_workspace_path_error(path, fragment):
    assert fragment in (_workspace_path_error(path) or "")


@pytest.mark.parametrize("path", ["/workspace/tmp/report.md", "/workspace/repo/docs/report.html", "/workspace/x"])
def test_workspace_path_accepted(path):
    assert _workspace_path_error(path) is None


async def test_publish_hands_the_file_to_the_store_and_returns_its_result():
    store = FakeArtifactStore()
    backend = _FakeBackend({"/workspace/tmp/audit.html": b"<h1>Audit</h1>"})

    result = await _tool(FakeWorkspace(backend=backend), store)(
        path=" /workspace/tmp/audit.html ", runtime=_runtime(THREAD_ID), title="Audit"
    )

    assert backend.requested == ["/workspace/tmp/audit.html"]
    assert store.stored == [
        {"thread_id": THREAD_ID, "filename": "audit.html", "content": b"<h1>Audit</h1>", "title": "Audit"}
    ]
    assert json.loads(result) == {"status": "published", "filename": "audit.html"}


async def test_publish_rejects_path_outside_workspace_without_touching_backend():
    backend = _FakeBackend({"/etc/passwd": b"root"})

    result = await _tool(FakeWorkspace(backend=backend))(path="/etc/passwd", runtime=_runtime(THREAD_ID))

    assert result.startswith("Error publishing artifact:")
    assert "outside /workspace" in result
    assert backend.requested == []


@pytest.mark.parametrize(("thread_id", "asked"), [(THREAD_ID, [THREAD_ID]), (None, [])])
async def test_publish_without_a_run_reports_no_session_and_reads_nothing(thread_id, asked):
    store = FakeArtifactStore(accepts=False)
    backend = _FakeBackend({"/workspace/tmp/r.md": b"# r"})

    result = await _tool(FakeWorkspace(backend=backend), store)(path="/workspace/tmp/r.md", runtime=_runtime(thread_id))

    assert result == NO_SESSION
    assert (backend.requested, store.asked, store.stored) == ([], asked, [])


async def test_publish_missing_file_tells_agent_to_write_it_first():
    store = FakeArtifactStore()

    result = await _tool(FakeWorkspace(backend=_FakeBackend()), store)(
        path="/workspace/tmp/missing.md", runtime=_runtime(THREAD_ID)
    )

    assert "does not exist" in result
    assert store.stored == []


@pytest.mark.parametrize(
    ("error", "hint"),
    [
        ("is_directory", "is a directory"),
        ("permission_denied", "chmod"),
        ("file_too_large", "artifact limit"),
        ("reading the file timed out in the sandbox", "timed out in the sandbox. Do not retry"),
    ],
)
async def test_publish_download_errors_carry_actionable_hints(error, hint):
    result = await _tool(FakeWorkspace(backend=_FakeBackend(error=error)))(
        path="/workspace/tmp/reports", runtime=_runtime(THREAD_ID)
    )

    assert hint in result


async def test_publish_transport_failure_is_a_soft_error():
    backend = Mock()
    backend.adownload_files = AsyncMock(side_effect=httpx.ConnectError("boom"))

    result = await _tool(FakeWorkspace(backend=backend))(path="/workspace/tmp/r.md", runtime=_runtime(THREAD_ID))

    assert result.startswith("Error publishing artifact '/workspace/tmp/r.md'")
    assert "retry" in result


async def test_a_store_rejection_becomes_the_tools_error_result():
    store = FakeArtifactStore(error=ArtifactError("'empty.md' is empty; write the file before publishing it."))
    backend = _FakeBackend({"/workspace/tmp/empty.md": b""})

    result = await _tool(FakeWorkspace(backend=backend), store)(
        path="/workspace/tmp/empty.md", runtime=_runtime(THREAD_ID)
    )

    assert result == (
        "Error publishing artifact '/workspace/tmp/empty.md': 'empty.md' is empty; write the file before publishing it."
    )


@pytest.mark.parametrize("failing", ["download", "store"])
async def test_publish_unexpected_failure_is_logged_and_returned_not_raised(caplog, failing):
    store = FakeArtifactStore()
    backend = _FakeBackend({"/workspace/tmp/r.md": b"# r"})
    if failing == "download":
        backend.adownload_files = AsyncMock(side_effect=OSError("File name too long"))
    else:
        store.error = OSError("disk full")

    result = await _tool(FakeWorkspace(backend=backend), store)(path="/workspace/tmp/r.md", runtime=_runtime(THREAD_ID))

    assert result.startswith("Error publishing artifact '/workspace/tmp/r.md': DAIV could not store the file")
    assert "unexpected failure publishing" in caplog.text
    assert store.stored == []


async def test_awrap_model_call_appends_artifacts_prompt():
    middleware = ArtifactsMiddleware(workspace=FakeWorkspace(), store=FakeArtifactStore())
    request = Mock(system_prompt="BASE")
    request.override = Mock(return_value="overridden")
    handler = AsyncMock(return_value="response")

    assert await middleware.awrap_model_call(request, handler) == "response"

    request.override.assert_called_once_with(system_prompt=f"BASE\n\n{ARTIFACTS_SYSTEM_PROMPT}")
    handler.assert_awaited_once_with("overridden")


async def test_publish_caps_the_download_at_the_store_limit():
    path = "/workspace/tmp/huge.log"
    workspace = FakeWorkspace()
    workspace.download_file = AsyncMock(
        return_value=FileDownloadResponse(path=path, content=None, error=DOWNLOAD_TOO_LARGE)
    )

    result = await _tool(workspace, FakeArtifactStore(max_bytes=1234))(path=path, runtime=_runtime(THREAD_ID))

    workspace.download_file.assert_awaited_once_with(path, max_bytes=1234)
    assert "larger than the 1234-byte artifact limit" in result
