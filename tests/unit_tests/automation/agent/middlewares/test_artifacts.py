from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from deepagents.backends.protocol import FILE_NOT_FOUND, FileDownloadResponse, FileUploadResponse
from langchain.tools import ToolRuntime

from automation.agent.artifacts import FETCH_ARTIFACT_TOOL_NAME, PUBLISH_ARTIFACT_TOOL_NAME, ArtifactError, ArtifactFile
from automation.agent.middlewares.artifacts import (
    ARTIFACTS_SYSTEM_PROMPT,
    ArtifactsMiddleware,
    _fetch_path_error,
    _workspace_path_error,
)
from automation.agent.workspace.sandbox_backend import DOWNLOAD_TOO_LARGE
from tests.unit_tests.conftest import FakeArtifactStore, FakeWorkspace

THREAD_ID = "thread-1"
NO_SESSION = (
    "Error publishing artifact: this run has no session to attach artifacts to. "
    "Put the key content in your final response instead."
)
ARTIFACT_ID = "3f1c2b6e-0d7a-4f0e-9c4b-2a8d6e1f5b70"
DEFAULT_DESCRIPTION_SHA256 = "312b0af67e9b2e5616bff5cc06a310d43f0cedea330d4e3b78d6cd2a3d8537fc"


class _FakeBackend:
    """A ``/workspace`` backend over an in-memory file map, which ``adownload_files`` serves and ``aupload_files``
    fills."""

    def __init__(
        self, files: dict[str, bytes] | None = None, *, error: str | None = None, upload_error: str | None = None
    ):
        self.files = files or {}
        self.error = error
        self.upload_error = upload_error
        self.requested: list[str] = []

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        if self.upload_error is None:
            self.files.update(files)
        return [FileUploadResponse(path=path, error=self.upload_error) for path, _ in files]

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


def _named_tool(name: str, workspace, store: FakeArtifactStore | None = None):
    middleware = ArtifactsMiddleware(workspace=workspace, store=store or FakeArtifactStore())
    (tool,) = [t for t in middleware.tools if t.name == name]
    return tool


def _tool(workspace, store: FakeArtifactStore | None = None):
    """The publish tool's coroutine over ``workspace`` and ``store`` (an accepting fake unless given)."""
    return _named_tool(PUBLISH_ARTIFACT_TOOL_NAME, workspace, store).coroutine


def _fetch_tool(workspace, store: FakeArtifactStore | None = None):
    """The fetch tool's coroutine over ``workspace`` and ``store`` (an accepting fake unless given)."""
    return _named_tool(FETCH_ARTIFACT_TOOL_NAME, workspace, store).coroutine


def test_middleware_adds_the_publish_and_fetch_tools():
    middleware = ArtifactsMiddleware(workspace=FakeWorkspace(), store=FakeArtifactStore())
    assert [t.name for t in middleware.tools] == [PUBLISH_ARTIFACT_TOOL_NAME, FETCH_ARTIFACT_TOOL_NAME]


@pytest.mark.parametrize(
    ("name", "properties", "required"),
    [
        (PUBLISH_ARTIFACT_TOOL_NAME, {"path", "title", "artifact_id"}, ["path"]),
        (FETCH_ARTIFACT_TOOL_NAME, {"artifact_id", "path"}, ["artifact_id"]),
    ],
)
def test_tool_call_schemas_hide_runtime_and_require_only_their_target(name, properties, required):
    schema = _named_tool(name, FakeWorkspace()).tool_call_schema.model_json_schema()
    assert set(schema["properties"]) == properties
    assert schema["required"] == required


def test_description_quotes_the_store_limits():
    store = FakeArtifactStore(max_bytes=1234, per_run_max=7)
    tool = ArtifactsMiddleware(workspace=FakeWorkspace(), store=store).tools[0]
    assert "Limits: 1234 bytes per file, 7 files per run." in tool.description


def test_description_at_the_default_limits_is_pinned():
    """The tools array is part of every cached prompt, so the text at the default limits only changes on purpose."""
    store = FakeArtifactStore(max_bytes=10 * 1024 * 1024, per_run_max=20)
    tool = ArtifactsMiddleware(workspace=FakeWorkspace(), store=store).tools[0]
    assert hashlib.sha256(tool.description.encode()).hexdigest() == DEFAULT_DESCRIPTION_SHA256


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
        {
            "thread_id": THREAD_ID,
            "filename": "audit.html",
            "content": b"<h1>Audit</h1>",
            "title": "Audit",
            "artifact_id": None,
        }
    ]
    assert json.loads(result) == {"status": "published", "filename": "audit.html"}


async def test_publish_with_an_artifact_id_hands_the_store_a_revision():
    store = FakeArtifactStore()
    backend = _FakeBackend({"/workspace/tmp/audit.html": b"<h1>Audit v2</h1>"})

    result = await _tool(FakeWorkspace(backend=backend), store)(
        path="/workspace/tmp/audit.html", runtime=_runtime(THREAD_ID), artifact_id=f" {ARTIFACT_ID} "
    )

    assert [(s["artifact_id"], s["content"]) for s in store.stored] == [(ARTIFACT_ID, b"<h1>Audit v2</h1>")]
    assert json.loads(result) == {"status": "updated", "filename": "audit.html"}


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


@pytest.mark.parametrize(
    ("path", "error"),
    [
        ("/workspace/tmp/report.md", False),
        ("/workspace/tmp/nested/report.md", False),
        ("/workspace/tmp", True),
        ("/workspace/repo/report.md", True),
        ("/workspace/tmp/../repo/report.md", True),
        ("tmp/report.md", True),
    ],
)
def test_fetch_path_error(path, error):
    assert (_fetch_path_error(path) is not None) is error


async def test_fetch_writes_the_artifact_to_its_file_name_in_tmp_by_default():
    store = FakeArtifactStore(artifacts={ARTIFACT_ID: ArtifactFile("audit.html", b"<h1>Audit</h1>")})
    backend = _FakeBackend()

    result = await _fetch_tool(FakeWorkspace(backend=backend), store)(
        artifact_id=f" {ARTIFACT_ID} ", runtime=_runtime(THREAD_ID)
    )

    assert store.read == [(THREAD_ID, ARTIFACT_ID)]
    assert backend.files == {"/workspace/tmp/audit.html": b"<h1>Audit</h1>"}
    assert result.startswith(f"Fetched artifact '{ARTIFACT_ID}' (audit.html, 14 bytes) to /workspace/tmp/audit.html.")
    assert f'publish_artifact(path="/workspace/tmp/audit.html", artifact_id="{ARTIFACT_ID}")' in result


async def test_fetch_writes_to_the_given_path():
    store = FakeArtifactStore(artifacts={ARTIFACT_ID: ArtifactFile("audit.html", b"<h1>Audit</h1>")})
    backend = _FakeBackend()

    await _fetch_tool(FakeWorkspace(backend=backend), store)(
        artifact_id=ARTIFACT_ID, path="/workspace/tmp/v2/audit.html", runtime=_runtime(THREAD_ID)
    )

    assert backend.files == {"/workspace/tmp/v2/audit.html": b"<h1>Audit</h1>"}


async def test_fetch_rejects_a_path_outside_tmp_before_reading_the_artifact():
    store = FakeArtifactStore(artifacts={ARTIFACT_ID: ArtifactFile("audit.html", b"x")})
    backend = _FakeBackend()

    result = await _fetch_tool(FakeWorkspace(backend=backend), store)(
        artifact_id=ARTIFACT_ID, path="/workspace/repo/audit.html", runtime=_runtime(THREAD_ID)
    )

    assert result.startswith(f"Error fetching artifact '{ARTIFACT_ID}': '/workspace/repo/audit.html' must be")
    assert (store.read, backend.files) == ([], {})


async def test_fetch_without_a_run_reads_nothing():
    store = FakeArtifactStore(accepts=False, artifacts={ARTIFACT_ID: ArtifactFile("audit.html", b"x")})

    result = await _fetch_tool(FakeWorkspace(backend=_FakeBackend()), store)(
        artifact_id=ARTIFACT_ID, runtime=_runtime(THREAD_ID)
    )

    assert result == "Error fetching artifact: this run has no session to fetch artifacts from."
    assert store.read == []


async def test_fetch_of_an_artifact_the_session_lacks_is_the_stores_error():
    result = await _fetch_tool(FakeWorkspace(backend=_FakeBackend()))(
        artifact_id=ARTIFACT_ID, runtime=_runtime(THREAD_ID)
    )

    assert result == f"Error fetching artifact '{ARTIFACT_ID}': this session has no artifact '{ARTIFACT_ID}'."


async def test_fetch_reports_a_failed_write():
    store = FakeArtifactStore(artifacts={ARTIFACT_ID: ArtifactFile("audit.html", b"x")})
    backend = _FakeBackend(upload_error="permission_denied")

    result = await _fetch_tool(FakeWorkspace(backend=backend), store)(
        artifact_id=ARTIFACT_ID, runtime=_runtime(THREAD_ID)
    )

    assert result == (
        f"Error fetching artifact '{ARTIFACT_ID}': could not write '/workspace/tmp/audit.html': permission_denied."
    )


async def test_fetch_transport_failure_is_a_soft_error():
    store = FakeArtifactStore(artifacts={ARTIFACT_ID: ArtifactFile("audit.html", b"x")})
    backend = Mock()
    backend.aupload_files = AsyncMock(side_effect=httpx.ConnectError("boom"))

    result = await _fetch_tool(FakeWorkspace(backend=backend), store)(
        artifact_id=ARTIFACT_ID, runtime=_runtime(THREAD_ID)
    )

    assert result.startswith(f"Error fetching artifact '{ARTIFACT_ID}'")
    assert "retry" in result


async def test_fetch_unexpected_failure_is_logged_and_returned_not_raised(caplog):
    store = FakeArtifactStore(error=OSError("disk gone"))

    result = await _fetch_tool(FakeWorkspace(backend=_FakeBackend()), store)(
        artifact_id=ARTIFACT_ID, runtime=_runtime(THREAD_ID)
    )

    assert result.startswith(f"Error fetching artifact '{ARTIFACT_ID}': DAIV could not read the artifact")
    assert "unexpected failure fetching" in caplog.text
