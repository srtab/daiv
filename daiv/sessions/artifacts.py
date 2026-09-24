"""Run artifacts: files the agent publishes out of its workspace so a person can open them.

Rendering is keyed on ``content_type``, derived from the file extension by ``guess_content_type``
rather than sniffed from the bytes: the browser is told what to expect, ``nosniff`` holds it to
that, and the raw endpoint serves every artifact under a ``sandbox`` CSP so agent-authored HTML
never runs in DAIV's origin.
"""

from __future__ import annotations

import logging
import mimetypes
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from django.core.files.base import ContentFile
from django.db import models, transaction
from django.utils.translation import gettext_lazy as _

from asgiref.sync import sync_to_async
from pydantic import BaseModel

from sessions.conf import settings

if TYPE_CHECKING:
    import uuid
    from collections.abc import Iterator

    from sessions.models import Run, RunArtifact

logger = logging.getLogger("daiv.sessions")


class ArtifactError(ValueError):
    """A publish request the agent can act on: bad file name, empty or oversized file, run budget spent."""


class ArtifactKind(models.TextChoices):
    """How the viewer presents an artifact."""

    MARKDOWN = "markdown", _("Markdown")
    HTML = "html", _("HTML")
    IMAGE = "image", _("Image")
    TEXT = "text", _("Text")
    OTHER = "other", _("File")


class ArtifactPayload(BaseModel):
    """The wire shape shared by the Jobs API, MCP and the tool result."""

    id: str
    title: str
    filename: str
    content_type: str
    size: int
    url: str
    download_url: str


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

_active_run_id: ContextVar[str | None] = ContextVar("sessions_active_run_id", default=None)
_active_task_result_id: ContextVar[str | None] = ContextVar("sessions_active_task_result_id", default=None)


def guess_content_type(filename: str) -> str:
    ext = PurePosixPath(filename).suffix.lower()
    if ext in _CONTENT_TYPES:
        return _CONTENT_TYPES[ext]
    guessed, encoding = mimetypes.guess_type(filename)
    # ``report.html.gz`` guesses ``text/html`` with a gzip encoding; its bytes are gzip, not HTML.
    return DEFAULT_CONTENT_TYPE if encoding or not guessed else guessed


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


@contextmanager
def bind_active_run(run_id: str | uuid.UUID) -> Iterator[None]:
    """Name the ``Run`` the code inside executes, for :func:`aresolve_active_run`."""
    token = _active_run_id.set(str(run_id))
    try:
        yield
    finally:
        _active_run_id.reset(token)


def bind_active_task_result(task_result_id: str | uuid.UUID | None) -> None:
    """Record the task the worker thread is executing; ``None`` clears it once the task finishes."""
    _active_task_result_id.set(str(task_result_id) if task_result_id else None)


async def aresolve_active_run(thread_id: str) -> Run | None:
    """The run executing on ``thread_id`` right now.

    Chat turns and jobs bind their run id, and a webhook run is found through the task the worker is
    executing. Anything unbound falls back to the thread's newest RUNNING row, else its newest READY one.
    """
    from sessions.models import Run, RunStatus

    runs = Run.objects.filter(session_id=thread_id)
    if run_id := _active_run_id.get():
        if (run := await runs.filter(pk=run_id).afirst()) is None:
            logger.error("Bound run %s is not on thread %s", run_id, thread_id)
        return run
    if (task_result_id := _active_task_result_id.get()) and (
        run := await runs.filter(task_result_id=task_result_id).afirst()
    ):
        return run
    for status in (RunStatus.RUNNING, RunStatus.READY):
        if run := await runs.filter(status=status).order_by("-created_at").afirst():
            return run
    return None


async def astore_artifact(run: Run, *, filename: str, content: bytes, title: str = "") -> RunArtifact:
    """Persist ``content`` as an artifact of ``run``; raises :class:`ArtifactError` on a rejected file."""
    safe_name = PurePosixPath(filename).name
    if safe_name in ("", ".."):
        raise ArtifactError(f"'{filename}' is not a file name.")
    if not content:
        raise ArtifactError(f"'{safe_name}' is empty; write the file before publishing it.")
    if len(content) > settings.ARTIFACT_MAX_BYTES:
        raise ArtifactError(
            f"'{safe_name}' is {len(content)} bytes; artifacts are capped at {settings.ARTIFACT_MAX_BYTES} bytes."
        )
    return await sync_to_async(_store_artifact)(run, safe_name, content, title)


def _store_artifact(run: Run, safe_name: str, content: bytes, title: str) -> RunArtifact:
    from sessions.models import Run, RunArtifact

    artifact = RunArtifact(
        run=run,
        title=(title.strip() or safe_name)[:200],
        filename=safe_name[:255],
        content_type=guess_content_type(safe_name),
        size=len(content),
    )
    writing = False
    try:
        with transaction.atomic():
            # Parallel tool calls in one model turn publish concurrently; the lock serialises the budget check.
            Run.objects.select_for_update().only("pk").get(pk=run.pk)
            if RunArtifact.objects.filter(run=run).count() >= settings.ARTIFACTS_PER_RUN_MAX:
                raise ArtifactError(
                    f"This run already published {settings.ARTIFACTS_PER_RUN_MAX} artifacts, the maximum."
                )
            writing = True
            artifact.file.save(safe_name, ContentFile(content), save=False)
            artifact.save()
    except BaseException:
        if writing:
            _discard_file(artifact, safe_name)
        raise
    return artifact


def _discard_file(artifact: RunArtifact, safe_name: str) -> None:
    name = artifact.file.name or artifact.file.field.generate_filename(artifact, safe_name)
    try:
        artifact.file.storage.delete(name)
    except Exception:
        logger.exception("Failed to discard artifact file %s", name)


def serialize_artifact(artifact: RunArtifact) -> ArtifactPayload:
    """SYNC ONLY: the absolute URLs read the current ``Site``."""
    from core.utils import build_absolute_url

    return ArtifactPayload(
        id=str(artifact.id),
        title=artifact.title,
        filename=artifact.filename,
        content_type=artifact.content_type,
        size=artifact.size,
        url=build_absolute_url(artifact.get_absolute_url()),
        download_url=build_absolute_url(artifact.get_download_url()),
    )


async def aserialize_run_artifacts(run: Run) -> list[ArtifactPayload]:
    """``serialize_artifact`` for every artifact of ``run``, oldest first."""
    return await sync_to_async(lambda: [serialize_artifact(artifact) for artifact in run.artifacts.all()])()


async def aserialize_run_artifacts_for_status(run: Run) -> tuple[list[ArtifactPayload], str | None]:
    """Artifacts for a job-status response, which must still answer when the listing fails."""
    try:
        return await aserialize_run_artifacts(run), None
    except Exception:
        logger.exception("Failed to list artifacts for run=%s", run.pk)
        return [], "The run's artifacts could not be listed; retry the status request later."
