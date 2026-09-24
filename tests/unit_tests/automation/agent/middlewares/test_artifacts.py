from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, Mock

from django.contrib.sites.models import Site

import httpx
import pytest
from asgiref.sync import sync_to_async
from deepagents.backends.protocol import FILE_NOT_FOUND, FileDownloadResponse
from langchain.tools import ToolRuntime
from sessions.models import Run, RunArtifact, RunStatus, Session, SessionOrigin

from automation.agent.middlewares.artifacts import (
    ARTIFACTS_SYSTEM_PROMPT,
    PUBLISH_ARTIFACT_TOOL_NAME,
    ArtifactsMiddleware,
    _workspace_path_error,
)

pytestmark = pytest.mark.django_db(transaction=True)


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


def _session_with_running_run_sync() -> tuple[Session, Run]:
    session = Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.API_JOB, repo_id="group/repo", ref="main"
    )
    run = Run.objects.create(
        session=session, trigger_type=SessionOrigin.API_JOB, repo_id=session.repo_id, status=RunStatus.RUNNING
    )
    return session, run


_session_with_running_run = sync_to_async(_session_with_running_run_sync)


def _tool(backend) -> callable:
    middleware = ArtifactsMiddleware(backend=backend)
    (tool,) = middleware.tools
    assert tool.name == PUBLISH_ARTIFACT_TOOL_NAME
    return tool.coroutine


def test_tool_call_schema_hides_runtime_and_makes_title_optional():
    tool = ArtifactsMiddleware(backend=_FakeBackend()).tools[0]
    schema = tool.tool_call_schema.model_json_schema()
    assert set(schema["properties"]) == {"path", "title"}
    assert schema["required"] == ["path"]


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


async def test_publish_stores_artifact_and_returns_urls():
    await Site.objects.aupdate_or_create(pk=1, defaults={"domain": "daiv.example.com", "name": "DAIV"})
    session, run = await _session_with_running_run()
    backend = _FakeBackend({"/workspace/tmp/audit.html": b"<h1>Audit</h1>"})

    result = await _tool(backend)(
        path=" /workspace/tmp/audit.html ", runtime=_runtime(session.thread_id), title="Audit"
    )

    payload = json.loads(result)
    artifact = await RunArtifact.objects.aget(run=run)
    assert backend.requested == ["/workspace/tmp/audit.html"]
    assert payload["status"] == "published"
    assert payload["title"] == "Audit"
    assert payload["filename"] == "audit.html"
    assert payload["content_type"] == "text/html"
    assert payload["size"] == len(b"<h1>Audit</h1>")
    assert payload["url"] == f"https://daiv.example.com/dashboard/sessions/{session.thread_id}/artifacts/{artifact.pk}/"
    assert payload["download_url"].endswith(f"/artifacts/{artifact.pk}/raw/?download=1")


async def test_publish_rejects_path_outside_workspace_without_touching_backend():
    session, _run = await _session_with_running_run()
    backend = _FakeBackend({"/etc/passwd": b"root"})

    result = await _tool(backend)(path="/etc/passwd", runtime=_runtime(session.thread_id))

    assert result.startswith("Error publishing artifact:")
    assert "outside /workspace" in result
    assert backend.requested == []


async def test_publish_without_active_run_reports_no_session():
    backend = _FakeBackend({"/workspace/tmp/r.md": b"# r"})

    assert "no session" in await _tool(backend)(path="/workspace/tmp/r.md", runtime=_runtime(str(uuid.uuid4())))
    assert "no session" in await _tool(backend)(path="/workspace/tmp/r.md", runtime=_runtime(None))
    assert backend.requested == []


async def test_publish_missing_file_tells_agent_to_write_it_first():
    session, run = await _session_with_running_run()

    result = await _tool(_FakeBackend())(path="/workspace/tmp/missing.md", runtime=_runtime(session.thread_id))

    assert "does not exist" in result
    assert not await RunArtifact.objects.filter(run=run).aexists()


async def test_publish_surfaces_backend_error_string():
    session, _run = await _session_with_running_run()
    backend = _FakeBackend(error="permission denied by sandbox")

    result = await _tool(backend)(path="/workspace/tmp/r.md", runtime=_runtime(session.thread_id))

    assert "permission denied by sandbox" in result


async def test_publish_transport_failure_is_a_soft_error():
    session, _run = await _session_with_running_run()
    backend = Mock()
    backend.adownload_files = AsyncMock(side_effect=httpx.ConnectError("boom"))

    result = await _tool(backend)(path="/workspace/tmp/r.md", runtime=_runtime(session.thread_id))

    assert result.startswith("Error publishing artifact '/workspace/tmp/r.md'")
    assert "ConnectError" in result


async def test_publish_empty_file_is_rejected_by_store():
    session, run = await _session_with_running_run()
    backend = _FakeBackend({"/workspace/tmp/empty.md": b""})

    result = await _tool(backend)(path="/workspace/tmp/empty.md", runtime=_runtime(session.thread_id))

    assert "is empty" in result
    assert not await RunArtifact.objects.filter(run=run).aexists()


async def test_awrap_model_call_appends_artifacts_prompt():
    middleware = ArtifactsMiddleware(backend=_FakeBackend())
    request = Mock(system_prompt="BASE")
    request.override = Mock(return_value="overridden")
    handler = AsyncMock(return_value="response")

    assert await middleware.awrap_model_call(request, handler) == "response"

    request.override.assert_called_once_with(system_prompt=f"BASE\n\n{ARTIFACTS_SYSTEM_PROMPT}")
    handler.assert_awaited_once_with("overridden")
