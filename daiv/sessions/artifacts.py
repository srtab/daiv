"""Run artifacts: files the agent publishes out of its workspace so a person can open them.

A run's workspace is discarded when it ends and its final message is the only text that survives,
so a generated report (HTML, Markdown, CSV, ...) had nowhere to go but the repository. The
``publish_artifact`` tool copies such a file into DAIV's file storage as a ``RunArtifact`` row on
the in-flight run. The sessions UI renders it (Markdown server-side, HTML inside a sandboxed
frame, images inline, anything else as a download), and the Jobs API / MCP responses list it
with its URLs.

Rendering is keyed on ``content_type``, derived from the file extension by ``guess_content_type``
rather than sniffed from the bytes: the browser is told what to expect, ``nosniff`` holds it to
that, and the raw endpoint serves every artifact under a ``sandbox`` CSP so agent-authored HTML
never runs in DAIV's origin.
"""

from __future__ import annotations

import mimetypes
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from django.core.files.base import ContentFile
from django.utils.translation import gettext_lazy as _

from asgiref.sync import sync_to_async

from sessions.conf import settings

if TYPE_CHECKING:
    from sessions.models import Run, RunArtifact


class ArtifactError(ValueError):
    """A publish request the agent can act on: bad path, empty or oversized file, run budget spent."""


class ArtifactKind(StrEnum):
    """How the viewer presents an artifact."""

    MARKDOWN = "markdown"
    HTML = "html"
    IMAGE = "image"
    TEXT = "text"
    OTHER = "other"


ARTIFACT_KIND_LABELS = {
    ArtifactKind.MARKDOWN: _("Markdown"),
    ArtifactKind.HTML: _("HTML"),
    ArtifactKind.IMAGE: _("Image"),
    ArtifactKind.TEXT: _("Text"),
    ArtifactKind.OTHER: _("File"),
}


_CONTENT_TYPES = {
    ".html": "text/html",
    ".htm": "text/html",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}

_TEXT_CONTENT_TYPES = frozenset({
    "text/plain",
    "text/csv",
    "text/tab-separated-values",
    "application/json",
    "application/xml",
    "text/xml",
    "application/yaml",
})

_IMAGE_CONTENT_TYPES = frozenset({"image/svg+xml", "image/png", "image/jpeg", "image/gif", "image/webp"})

DEFAULT_CONTENT_TYPE = "application/octet-stream"


def guess_content_type(filename: str) -> str:
    ext = PurePosixPath(filename).suffix.lower()
    if ext in _CONTENT_TYPES:
        return _CONTENT_TYPES[ext]
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or DEFAULT_CONTENT_TYPE


def artifact_kind(content_type: str) -> ArtifactKind:
    if content_type == "text/markdown":
        return ArtifactKind.MARKDOWN
    if content_type == "text/html":
        return ArtifactKind.HTML
    if content_type in _IMAGE_CONTENT_TYPES:
        return ArtifactKind.IMAGE
    if content_type in _TEXT_CONTENT_TYPES:
        return ArtifactKind.TEXT
    return ArtifactKind.OTHER


async def aresolve_active_run(thread_id: str) -> Run | None:
    """The run executing on ``thread_id`` right now, whichever way it was triggered.

    Chat turns, background jobs and webhook runs all hold the session lock under different
    holder ids, so the ``Run`` row is found by status instead: the newest RUNNING row, else
    the newest READY one (a job whose ``task_started`` sync has not landed yet).
    """
    from sessions.models import Run, RunStatus

    for status in (RunStatus.RUNNING, RunStatus.READY):
        run = await Run.objects.filter(session_id=thread_id, status=status).order_by("-created_at").afirst()
        if run is not None:
            return run
    return None


async def astore_artifact(run: Run, *, filename: str, content: bytes, title: str = "") -> RunArtifact:
    """Persist ``content`` as an artifact of ``run``; raises :class:`ArtifactError` on a rejected file."""
    from sessions.models import RunArtifact

    safe_name = PurePosixPath(filename).name
    if not safe_name or safe_name in {".", ".."}:
        raise ArtifactError(f"'{filename}' is not a file name.")
    if not content:
        raise ArtifactError(f"'{safe_name}' is empty; write the file before publishing it.")
    if len(content) > settings.ARTIFACT_MAX_BYTES:
        raise ArtifactError(
            f"'{safe_name}' is {len(content)} bytes; artifacts are capped at {settings.ARTIFACT_MAX_BYTES} bytes."
        )
    if await RunArtifact.objects.filter(run=run).acount() >= settings.ARTIFACTS_PER_RUN_MAX:
        raise ArtifactError(f"This run already published {settings.ARTIFACTS_PER_RUN_MAX} artifacts, the maximum.")

    artifact = RunArtifact(
        run=run,
        title=(title.strip() or safe_name)[:200],
        filename=safe_name[:255],
        content_type=guess_content_type(safe_name),
        size=len(content),
    )
    await sync_to_async(artifact.file.save)(safe_name, ContentFile(content), save=False)
    await artifact.asave()
    return artifact


def absolute_artifact_urls(artifact: RunArtifact) -> dict[str, str]:
    """``{"url", "download_url"}`` as absolute links (Site domain), for tool results and API responses."""
    from core.utils import build_absolute_url

    return {
        "url": build_absolute_url(artifact.get_absolute_url()),
        "download_url": build_absolute_url(artifact.get_download_url()),
    }


def serialize_artifact(artifact: RunArtifact) -> dict[str, object]:
    """The wire shape shared by the Jobs API, MCP and the tool result. Needs ``artifact.run`` loaded."""
    return {
        "id": str(artifact.id),
        "title": artifact.title,
        "filename": artifact.filename,
        "content_type": artifact.content_type,
        "size": artifact.size,
        **absolute_artifact_urls(artifact),
    }


async def aserialize_run_artifacts(run: Run) -> list[dict[str, object]]:
    """``serialize_artifact`` for every artifact of ``run``, oldest first; one sync hop for the Site lookup."""
    artifacts = [artifact async for artifact in run.artifacts.select_related("run").order_by("created_at", "id")]
    if not artifacts:
        return []
    return await sync_to_async(lambda: [serialize_artifact(artifact) for artifact in artifacts])()
