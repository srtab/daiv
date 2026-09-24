"""The ``publish_artifact`` tool: copy a workspace file into DAIV as a ``sessions.RunArtifact`` that outlives a run."""

from __future__ import annotations

import json
import logging
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Annotated

from asgiref.sync import sync_to_async
from deepagents.backends.protocol import FILE_NOT_FOUND
from httpx import HTTPError
from langchain.agents.middleware import AgentMiddleware
from langchain.tools import ToolRuntime  # noqa: TC002
from langchain_core.tools import BaseTool, tool
from sessions.artifacts import ArtifactError, aresolve_active_run, astore_artifact, serialize_artifact
from sessions.conf import settings as sessions_settings

from automation.agent.constants import TMP_PATH, WORKSPACE_PATH
from automation.agent.middlewares.file_system import (
    DOWNLOAD_TOO_LARGE,
    DAIVCompositeBackend,
    SandboxFileBackend,
    _fs_transport_failure_text,
)
from automation.agent.utils import conversation_thread_id
from codebase.context import RuntimeCtx  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from deepagents.backends.protocol import BackendProtocol, FileDownloadResponse
    from langchain.agents.middleware import ModelRequest, ModelResponse
    from sessions.models import RunArtifact

logger = logging.getLogger("daiv.tools")

PUBLISH_ARTIFACT_TOOL_NAME = "publish_artifact"

PUBLISH_ARTIFACT_TOOL_DESCRIPTION = f"""\
Publish a workspace file as a run artifact the user can open, view and download from DAIV after the run ends.

Use it for deliverables that are documents rather than code: an HTML or Markdown report, a CSV/JSON dataset, \
a chart image, a log. The file is copied into DAIV's storage and the tool returns its URL — include that URL \
in your final response so the user can reach it.

Rules:
  - `path` is an absolute path under `{WORKSPACE_PATH}` (usually `{TMP_PATH}/...`). Write the file first with \
`write_file` or `bash`.
  - Publishing does NOT commit the file to the repository; prefer it over committing generated reports.
  - Rendered in the browser: `.md` (Markdown), `.html` (in a sandboxed frame — inline CSS/JS is fine), images \
(`.png`, `.svg`, ...), plain text / CSV / JSON. Any other type is offered as a download.
  - Limits: {sessions_settings.ARTIFACT_MAX_BYTES} bytes per file, {sessions_settings.ARTIFACTS_PER_RUN_MAX} files \
per run.

Examples:
  - `{PUBLISH_ARTIFACT_TOOL_NAME}(path="{TMP_PATH}/dependency-audit.html", title="Dependency audit")`
  - `{PUBLISH_ARTIFACT_TOOL_NAME}(path="{TMP_PATH}/findings.md")`"""

ARTIFACTS_SYSTEM_PROMPT = f"""\
## Artifacts (`{PUBLISH_ARTIFACT_TOOL_NAME}`)

When a task's deliverable is a document rather than a code change — a report, an audit, a summary table, a \
chart — write it to `{TMP_PATH}/<name>.<ext>` and publish it with `{PUBLISH_ARTIFACT_TOOL_NAME}` (load it via \
`tool_search` if it is not loaded). DAIV stores the file and renders it at the returned URL: Markdown, HTML, \
images and plain text / CSV / JSON show in the browser, other types download. Put that URL in your final \
response. Do not commit generated reports to the repository unless the user asked for that; publishing is the \
default. Pick HTML when the report benefits from layout, styled tables or charts, and Markdown for prose findings."""

_WORKSPACE = PurePosixPath(WORKSPACE_PATH)
_GIVE_UP_ADVICE = "Do not retry; put the key content in your final response instead."
_INTERNAL_FAILURE = (
    f"DAIV could not store the file (a server-side failure, not a problem with the file). {_GIVE_UP_ADVICE}"
)
_DOWNLOAD_ERROR_HINTS = {
    FILE_NOT_FOUND: "the file does not exist. Write it first, then publish.",
    "is_directory": "it is a directory. Publish a single file (archive a directory first if needed).",
    "permission_denied": "the file is not readable. Fix its permissions (`chmod`), then publish.",
    DOWNLOAD_TOO_LARGE: (
        f"the file is larger than the {sessions_settings.ARTIFACT_MAX_BYTES}-byte artifact limit. "
        "Make it smaller, then publish."
    ),
}


def _workspace_path_error(path: str) -> str | None:
    """Reject a relative path, a ``..`` segment, and anything not strictly inside ``/workspace``."""
    pure = PurePosixPath(path)
    if not pure.is_absolute() or ".." in pure.parts:
        return f"'{path}' must be an absolute path under {WORKSPACE_PATH} (no '..' segments)."
    if _WORKSPACE not in pure.parents:
        return f"'{path}' is outside {WORKSPACE_PATH}; only workspace files can be published."
    return None


class ArtifactsMiddleware(AgentMiddleware):
    """Adds ``publish_artifact`` backed by the run's ``/workspace`` filesystem backend."""

    def __init__(self, *, backend: BackendProtocol) -> None:
        self._backend = backend
        self.tools = [self._build_tool()]

    def _build_tool(self) -> BaseTool:
        @tool(PUBLISH_ARTIFACT_TOOL_NAME, description=PUBLISH_ARTIFACT_TOOL_DESCRIPTION)
        async def publish_artifact_tool(
            path: Annotated[str, "Absolute path of the file to publish, under /workspace."],
            runtime: ToolRuntime[RuntimeCtx],
            title: Annotated[str, "Short human-readable title shown in DAIV; defaults to the file name."] = "",
        ) -> str:
            """Copy a workspace file into DAIV as a run artifact and return its URL."""
            path = path.strip()
            try:
                return await self._apublish(path, title, runtime)
            except Exception:
                logger.exception("publish_artifact: unexpected failure publishing %s", path)
                return f"Error publishing artifact '{path}': {_INTERNAL_FAILURE}"

        return publish_artifact_tool

    async def _apublish(self, path: str, title: str, runtime: ToolRuntime[RuntimeCtx]) -> str:
        if path_error := _workspace_path_error(path):
            return f"Error publishing artifact: {path_error}"

        thread_id = (runtime.config.get("configurable") or {}).get("thread_id") or conversation_thread_id()
        run = await aresolve_active_run(thread_id) if thread_id else None
        if run is None:
            logger.warning("publish_artifact: no active run for thread_id=%s", thread_id)
            return (
                "Error publishing artifact: this run has no session to attach artifacts to. "
                "Put the key content in your final response instead."
            )

        try:
            downloaded = await self._adownload(path)
        except HTTPError as exc:
            return f"Error publishing artifact '{path}': {_fs_transport_failure_text(exc, 'publish', path)}"
        if downloaded.error or downloaded.content is None:
            reason = downloaded.error or "the file could not be read"
            hint = _DOWNLOAD_ERROR_HINTS.get(reason) or f"{reason}. {_GIVE_UP_ADVICE}"
            return f"Error publishing artifact '{path}': {hint}"

        try:
            artifact = await astore_artifact(
                run, filename=PurePosixPath(path).name, content=downloaded.content, title=title
            )
        except ArtifactError as exc:
            return f"Error publishing artifact '{path}': {exc}"
        logger.info(
            "publish_artifact: run=%s stored %s (%s, %d bytes)", run.pk, path, artifact.content_type, artifact.size
        )
        return await self._apublished_result(artifact)

    async def _adownload(self, path: str) -> FileDownloadResponse:
        backend, key = self._backend.route(path) if isinstance(self._backend, DAIVCompositeBackend) else (None, path)
        if isinstance(backend, SandboxFileBackend):
            # Refused inside the sandbox, so an oversized file never crosses the wire.
            (downloaded,) = await backend.adownload_files([key], max_bytes=sessions_settings.ARTIFACT_MAX_BYTES)
        else:
            (downloaded,) = await self._backend.adownload_files([path])
        return downloaded

    @staticmethod
    async def _apublished_result(artifact: RunArtifact) -> str:
        try:
            payload = await sync_to_async(serialize_artifact)(artifact)
        except Exception:
            logger.exception("publish_artifact: stored artifact %s but could not build its absolute URLs", artifact.pk)
            return json.dumps({
                "status": "published",
                "id": str(artifact.pk),
                "url": artifact.get_absolute_url(),
                "warning": "Stored, but DAIV could not build absolute URLs; the URL is relative to the DAIV host.",
            })
        return json.dumps({"status": "published", **payload.model_dump()})

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        system_prompt = f"{request.system_prompt}\n\n" if request.system_prompt else ""
        return await handler(request.override(system_prompt=system_prompt + ARTIFACTS_SYSTEM_PROMPT))
