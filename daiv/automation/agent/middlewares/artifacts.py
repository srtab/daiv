"""The ``publish_artifact`` tool: hand a workspace file to DAIV so it outlives the run.

Everything the agent writes lives in the sandbox (or the run's temp clone) and is gone when the run
ends; only the repository commit and the final message survive. A generated report therefore had
to be either pasted into the reply or committed. This middleware adds the third way: the tool
copies a file out of the workspace through the run's filesystem backend and stores it as a
``sessions.RunArtifact`` on the in-flight run, returning the URL DAIV renders it at.

The tool is bound like any other DAIV tool and is deferred behind ``tool_search`` (it is not in
``ALWAYS_LOADED_TOOLS``); the system-prompt section below tells the model when to reach for it.
"""

from __future__ import annotations

import json
import logging
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Annotated

from asgiref.sync import sync_to_async
from deepagents.backends.protocol import FILE_NOT_FOUND
from httpx import HTTPError
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.tools import ToolRuntime  # noqa: TC002
from langchain_core.tools import BaseTool, tool
from sessions.artifacts import ArtifactError, aresolve_active_run, astore_artifact, serialize_artifact
from sessions.conf import settings as sessions_settings

from automation.agent.constants import TMP_PATH, WORKSPACE_PATH
from automation.agent.utils import conversation_thread_id
from codebase.context import RuntimeCtx  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from deepagents.backends.protocol import BackendProtocol

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
`tool_search` if it is not loaded). DAIV stores the file and renders it at the returned URL: Markdown and HTML \
render in the browser, images display inline, other types download. Put that URL in your final response. Do not \
commit generated reports to the repository unless the user asked for that; publishing is the default. Pick HTML \
when the report benefits from layout, styled tables or charts, and Markdown for prose findings."""


def _workspace_path_error(path: str) -> str | None:
    """Reject anything that is not an absolute, normalised path inside ``/workspace``."""
    pure = PurePosixPath(path.strip())
    if not pure.is_absolute() or ".." in pure.parts:
        return f"'{path}' must be an absolute path under {WORKSPACE_PATH} (no '..' segments)."
    if pure == PurePosixPath(WORKSPACE_PATH) or pure.parts[: len(PurePosixPath(WORKSPACE_PATH).parts)] != (
        PurePosixPath(WORKSPACE_PATH).parts
    ):
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
            if path_error := _workspace_path_error(path):
                return f"Error publishing artifact: {path_error}"

            thread_id = (runtime.config.get("configurable") or {}).get("thread_id") or conversation_thread_id()
            run = await aresolve_active_run(thread_id) if thread_id else None
            if run is None:
                return "Error publishing artifact: this run has no session to attach artifacts to."

            try:
                downloaded = (await self._backend.adownload_files([path]))[0]
            except HTTPError as exc:
                logger.warning("publish_artifact: download of %s failed: %s", path, exc)
                return f"Error publishing artifact '{path}': the workspace could not be read ({type(exc).__name__})."
            if downloaded.error == FILE_NOT_FOUND:
                return f"Error publishing artifact '{path}': the file does not exist. Write it first, then publish."
            if downloaded.error or downloaded.content is None:
                return f"Error publishing artifact '{path}': {downloaded.error or 'the file could not be read'}."

            try:
                artifact = await astore_artifact(
                    run, filename=PurePosixPath(path).name, content=downloaded.content, title=title
                )
            except ArtifactError as exc:
                return f"Error publishing artifact '{path}': {exc}"

            payload = await sync_to_async(serialize_artifact)(artifact)
            logger.info(
                "publish_artifact: run=%s stored %s (%s, %d bytes)", run.pk, path, artifact.content_type, artifact.size
            )
            return json.dumps({"status": "published", **payload})

        return publish_artifact_tool

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        system_prompt = f"{request.system_prompt}\n\n" if request.system_prompt else ""
        return await handler(request.override(system_prompt=system_prompt + ARTIFACTS_SYSTEM_PROMPT))
