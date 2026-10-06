"""The artifact tools: ``publish_artifact`` copies a workspace file into the run's ``ArtifactStore``, which keeps it
after the run, or revises an artifact the session already has; ``fetch_artifact`` copies one back for that revision.

The store decides where a file goes and the limits it takes (``sessions.artifacts.RunArtifactStore`` for executor
runs); the tool description and the too-large hint quote those limits, so each instance builds them.
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Annotated

from deepagents.backends.protocol import FILE_NOT_FOUND, IS_DIRECTORY, PERMISSION_DENIED
from httpx import HTTPError
from langchain.agents.middleware import AgentMiddleware
from langchain.tools import ToolRuntime  # noqa: TC002
from langchain_core.tools import BaseTool, tool

from automation.agent.artifacts import FETCH_ARTIFACT_TOOL_NAME, PUBLISH_ARTIFACT_TOOL_NAME, ArtifactError
from automation.agent.constants import TMP_PATH, WORKSPACE_PATH
from automation.agent.workspace.sandbox_backend import DOWNLOAD_TOO_LARGE, _fs_transport_failure_text, is_workspace_path
from codebase.context import RuntimeCtx  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware import ModelRequest, ModelResponse

    from automation.agent.artifacts import ArtifactStore
    from automation.agent.workspace.base import Workspace

logger = logging.getLogger("daiv.tools")


def _tool_description(*, max_bytes: int, per_run_max: int) -> str:
    return f"""\
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
  - To revise an artifact this session already published, pass its `id` as `artifact_id`: the file replaces the \
artifact's content and its URL stays the same. If the file is no longer in the workspace, copy it back with \
`{FETCH_ARTIFACT_TOOL_NAME}` first. Without `artifact_id`, every call publishes a new artifact.
  - Limits: {max_bytes} bytes per file, {per_run_max} files \
per run. Revisions do not count towards the file limit.

Examples:
  - `{PUBLISH_ARTIFACT_TOOL_NAME}(path="{TMP_PATH}/dependency-audit.html", title="Dependency audit")`
  - `{PUBLISH_ARTIFACT_TOOL_NAME}(path="{TMP_PATH}/findings.md")`
  - `{PUBLISH_ARTIFACT_TOOL_NAME}(path="{TMP_PATH}/findings.md", artifact_id="3f1c2b6e-0d7a-4f0e-9c4b-2a8d6e1f5b70")`"""


FETCH_ARTIFACT_DESCRIPTION = f"""\
Copy an artifact this session already published back into the workspace, so you can revise it and update it with \
`{PUBLISH_ARTIFACT_TOOL_NAME}(path=..., artifact_id=...)`.

Use it when the artifact's file is no longer in the workspace, e.g. it was published by an earlier run.

Rules:
  - `artifact_id` is the `id` from the artifact's publish result, or the UUID after `/artifacts/` in its URL.
  - The file is written to `path`, an absolute path under `{TMP_PATH}` (default: `{TMP_PATH}/<its file name>`), \
replacing any file already there.

Example:
  - `{FETCH_ARTIFACT_TOOL_NAME}(artifact_id="3f1c2b6e-0d7a-4f0e-9c4b-2a8d6e1f5b70")`"""


ARTIFACTS_SYSTEM_PROMPT = f"""\
## Artifacts (`{PUBLISH_ARTIFACT_TOOL_NAME}`)

When a task's deliverable is a document rather than a code change — a report, an audit, a summary table, a \
chart — write it to `{TMP_PATH}/<name>.<ext>` and publish it with `{PUBLISH_ARTIFACT_TOOL_NAME}` (load it via \
`tool_search` if it is not loaded). DAIV stores the file and renders it at the returned URL: Markdown, HTML, \
images and plain text / CSV / JSON show in the browser, other types download. Put that URL in your final \
response. Do not commit generated reports to the repository unless the user asked for that; publishing is the \
default. Pick HTML when the report benefits from layout, styled tables or charts, and Markdown for prose findings.

When the user asks to change an artifact this session already published, revise it instead of publishing a new \
one: edit its file (copy it back with `{FETCH_ARTIFACT_TOOL_NAME}` if it is no longer in the workspace) and publish \
it with `artifact_id` set to the artifact's `id`. Its URL stays the same."""

_GIVE_UP_ADVICE = "Do not retry; put the key content in your final response instead."
_INTERNAL_FAILURE = (
    f"DAIV could not store the file (a server-side failure, not a problem with the file). {_GIVE_UP_ADVICE}"
)
_NO_RUN_ERROR = (
    "Error publishing artifact: this run has no session to attach artifacts to. "
    "Put the key content in your final response instead."
)
_NO_RUN_FETCH_ERROR = "Error fetching artifact: this run has no session to fetch artifacts from."


def _download_error_hints(max_bytes: int) -> dict[str, str]:
    return {
        FILE_NOT_FOUND: "the file does not exist. Write it first, then publish.",
        IS_DIRECTORY: "it is a directory. Publish a single file (archive a directory first if needed).",
        PERMISSION_DENIED: "the file is not readable. Fix its permissions (`chmod`), then publish.",
        DOWNLOAD_TOO_LARGE: (
            f"the file is larger than the {max_bytes}-byte artifact limit. Make it smaller, then publish."
        ),
    }


def _workspace_path_error(path: str) -> str | None:
    if is_workspace_path(path):
        return None
    pure = PurePosixPath(path)
    if not pure.is_absolute() or ".." in pure.parts:
        return f"'{path}' must be an absolute path under {WORKSPACE_PATH} (no '..' segments)."
    return f"'{path}' is outside {WORKSPACE_PATH}; only workspace files can be published."


def _fetch_path_error(path: str) -> str | None:
    pure = PurePosixPath(path)
    if ".." not in pure.parts and PurePosixPath(TMP_PATH) in pure.parents:
        return None
    return f"'{path}' must be an absolute path under {TMP_PATH} (no '..' segments)."


def _thread_id(runtime: ToolRuntime[RuntimeCtx]) -> str | None:
    return (runtime.config.get("configurable") or {}).get("thread_id")


class ArtifactsMiddleware(AgentMiddleware):
    """Adds ``publish_artifact``, which copies a file out of the run's workspace into ``store``, and
    ``fetch_artifact``, which copies a stored artifact back into the workspace."""

    def __init__(self, *, workspace: Workspace, store: ArtifactStore) -> None:
        self._workspace = workspace
        self._store = store
        self._download_error_hints = _download_error_hints(store.max_bytes)
        self.tools = [
            self._build_publish_tool(_tool_description(max_bytes=store.max_bytes, per_run_max=store.per_run_max)),
            self._build_fetch_tool(),
        ]

    def _build_publish_tool(self, description: str) -> BaseTool:
        @tool(PUBLISH_ARTIFACT_TOOL_NAME, description=description)
        async def publish_artifact_tool(
            path: Annotated[str, "Absolute path of the file to publish, under /workspace."],
            runtime: ToolRuntime[RuntimeCtx],
            title: Annotated[str, "Short human-readable title shown in DAIV; defaults to the file name."] = "",
            artifact_id: Annotated[
                str, "The `id` of an artifact this session published, to replace its file; omit for a new artifact."
            ] = "",
        ) -> str:
            """Copy a workspace file into DAIV as a run artifact and return its URL."""
            path = path.strip()
            try:
                return await self._apublish(path, title, artifact_id.strip(), runtime)
            except Exception:
                logger.exception("publish_artifact: unexpected failure publishing %s", path)
                return f"Error publishing artifact '{path}': {_INTERNAL_FAILURE}"

        return publish_artifact_tool

    def _build_fetch_tool(self) -> BaseTool:
        @tool(FETCH_ARTIFACT_TOOL_NAME, description=FETCH_ARTIFACT_DESCRIPTION)
        async def fetch_artifact_tool(
            artifact_id: Annotated[str, "The `id` of an artifact this session published."],
            runtime: ToolRuntime[RuntimeCtx],
            path: Annotated[str, f"Where to write it, under {TMP_PATH}; defaults to {TMP_PATH}/<its file name>."] = "",
        ) -> str:
            """Copy a published artifact's file into the workspace."""
            artifact_id = artifact_id.strip()
            try:
                return await self._afetch(artifact_id, path.strip(), runtime)
            except Exception:
                logger.exception("fetch_artifact: unexpected failure fetching %s", artifact_id)
                return (
                    f"Error fetching artifact '{artifact_id}': DAIV could not read the artifact "
                    f"(a server-side failure). {_GIVE_UP_ADVICE}"
                )

        return fetch_artifact_tool

    async def _apublish(self, path: str, title: str, artifact_id: str, runtime: ToolRuntime[RuntimeCtx]) -> str:
        if path_error := _workspace_path_error(path):
            return f"Error publishing artifact: {path_error}"

        thread_id = _thread_id(runtime)
        if not thread_id or not await self._store.aaccepts(thread_id):
            logger.warning("publish_artifact: no active run for thread_id=%s", thread_id)
            return _NO_RUN_ERROR

        try:
            downloaded = await self._workspace.download_file(path, max_bytes=self._store.max_bytes)
        except HTTPError as exc:
            return f"Error publishing artifact '{path}': {_fs_transport_failure_text(exc, 'publish', path)}"
        if downloaded.error or downloaded.content is None:
            reason = downloaded.error or "the file could not be read"
            hint = self._download_error_hints.get(reason) or f"{reason}. {_GIVE_UP_ADVICE}"
            return f"Error publishing artifact '{path}': {hint}"

        try:
            return await self._store.astore(
                thread_id=thread_id,
                filename=PurePosixPath(path).name,
                content=downloaded.content,
                title=title,
                artifact_id=artifact_id or None,
            )
        except ArtifactError as exc:
            return f"Error publishing artifact '{path}': {exc}"

    async def _afetch(self, artifact_id: str, path: str, runtime: ToolRuntime[RuntimeCtx]) -> str:
        if path and (path_error := _fetch_path_error(path)):
            return f"Error fetching artifact '{artifact_id}': {path_error}"

        thread_id = _thread_id(runtime)
        if not thread_id or not await self._store.aaccepts(thread_id):
            logger.warning("fetch_artifact: no active run for thread_id=%s", thread_id)
            return _NO_RUN_FETCH_ERROR

        try:
            stored = await self._store.aread(thread_id=thread_id, artifact_id=artifact_id)
        except ArtifactError as exc:
            return f"Error fetching artifact '{artifact_id}': {exc}"

        path = path or f"{TMP_PATH}/{stored.filename}"
        try:
            (uploaded,) = await self._workspace.backend.aupload_files([(path, stored.content)])
        except HTTPError as exc:
            return f"Error fetching artifact '{artifact_id}': {_fs_transport_failure_text(exc, 'fetch', path)}"
        if uploaded.error:
            return f"Error fetching artifact '{artifact_id}': could not write '{path}': {uploaded.error}."
        return (
            f"Fetched artifact '{artifact_id}' ({stored.filename}, {len(stored.content)} bytes) to {path}. "
            f'Edit it, then call `{PUBLISH_ARTIFACT_TOOL_NAME}(path="{path}", artifact_id="{artifact_id}")` '
            "to update the artifact in place."
        )

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        system_prompt = f"{request.system_prompt}\n\n" if request.system_prompt else ""
        return await handler(request.override(system_prompt=system_prompt + ARTIFACTS_SYSTEM_PROMPT))
