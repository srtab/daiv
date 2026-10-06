"""Run artifacts: files the agent publishes out of its workspace so a person can open them.

Rendering is keyed on ``content_type``, derived from the file extension by ``guess_content_type``
rather than sniffed from the bytes: the browser is told what to expect, ``nosniff`` holds it to
that, and the raw endpoint serves every artifact under a ``sandbox`` CSP so agent-authored HTML
never runs in DAIV's origin.

``RunArtifactStore`` is the agent's ``automation.agent.artifacts.ArtifactStore`` for executor runs: a file goes to the
``Run`` that :func:`bind_active_run` names, within the ``sessions.conf`` limits. A revision replaces an artifact's file
in place: the row, and so its URL, stays, and it stays on the run that first published it; the revising run is added
to its ``revised_by``, so that run's job status lists it too.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from functools import partial
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Literal

from django.core.exceptions import ObjectDoesNotExist
from django.core.files.base import ContentFile
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from asgiref.sync import sync_to_async
from pydantic import BaseModel

from automation.agent.artifacts import ArtifactError, ArtifactFile
from sessions.conf import settings

if TYPE_CHECKING:
    from collections.abc import Generator

    from django.core.files.storage import Storage

    from sessions.models import Run, RunArtifact

logger = logging.getLogger("daiv.sessions")


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
    kind: ArtifactKind
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

_KIND_CONTENT_TYPES: dict[ArtifactKind, frozenset[str]] = {
    ArtifactKind.MARKDOWN: frozenset({"text/markdown"}),
    ArtifactKind.HTML: frozenset({"text/html"}),
    ArtifactKind.IMAGE: frozenset({"image/svg+xml", "image/png", "image/jpeg", "image/gif", "image/webp"}),
    ArtifactKind.TEXT: frozenset({
        "text/plain",
        "text/csv",
        "text/tab-separated-values",
        "application/json",
        "application/xml",
        "text/xml",
        "application/yaml",
    }),
    ArtifactKind.OTHER: frozenset(),
}

KNOWN_KIND_CONTENT_TYPES: frozenset[str] = frozenset().union(*_KIND_CONTENT_TYPES.values())

DEFAULT_CONTENT_TYPE = "application/octet-stream"

_active_run_id: ContextVar[str | None] = ContextVar("sessions_active_run_id", default=None)


def guess_content_type(filename: str) -> str:
    ext = PurePosixPath(filename).suffix.lower()
    if ext in _CONTENT_TYPES:
        return _CONTENT_TYPES[ext]
    guessed, encoding = mimetypes.guess_type(filename)
    # ``report.html.gz`` guesses ``text/html`` with a gzip encoding; its bytes are gzip, not HTML.
    return DEFAULT_CONTENT_TYPE if encoding or not guessed else guessed


def content_types_for_kind(kind: ArtifactKind) -> frozenset[str]:
    return _KIND_CONTENT_TYPES[kind]


def artifact_kind(content_type: str) -> ArtifactKind:
    for kind, content_types in _KIND_CONTENT_TYPES.items():
        if content_type in content_types:
            return kind
    return ArtifactKind.OTHER


@contextmanager
def bind_active_run(run_id: str | uuid.UUID | None) -> Generator[None]:
    """Name the ``Run`` the code inside executes, for :func:`aresolve_active_run`; ``None`` binds nothing."""
    if not run_id:
        yield
        return
    token = _active_run_id.set(str(run_id))
    try:
        yield
    finally:
        _active_run_id.reset(token)


async def aresolve_active_run(thread_id: str) -> Run | None:
    """The run executing on ``thread_id`` right now: the one its entry point bound, if it is on that thread.

    Unbound code gets ``None`` rather than a guess, since a RUNNING row on the thread may be a run
    still waiting on the session lock.
    """
    from sessions.models import Run

    if not (run_id := _active_run_id.get()):
        return None
    if (run := await Run.objects.filter(session_id=thread_id, pk=run_id).afirst()) is None:
        logger.error("Bound run %s is not on thread %s", run_id, thread_id)
    return run


def _checked_name(filename: str, content: bytes) -> str:
    safe_name = PurePosixPath(filename).name
    if safe_name in ("", ".."):
        raise ArtifactError(f"'{filename}' is not a file name.")
    if not content:
        raise ArtifactError(f"'{safe_name}' is empty; write the file before publishing it.")
    if len(content) > settings.ARTIFACT_MAX_BYTES:
        raise ArtifactError(
            f"'{safe_name}' is {len(content)} bytes; artifacts are capped at {settings.ARTIFACT_MAX_BYTES} bytes."
        )
    return safe_name


async def astore_artifact(run: Run, *, filename: str, content: bytes, title: str = "") -> RunArtifact:
    """Persist ``content`` as an artifact of ``run``; raises :class:`ArtifactError` on a rejected file."""
    safe_name = _checked_name(filename, content)
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
    try:
        artifact.file.save(safe_name, ContentFile(content), save=False)
        with transaction.atomic():
            # Parallel tool calls in one model turn publish concurrently; the lock serialises the budget check.
            Run.objects.select_for_update().only("pk").get(pk=run.pk)
            if RunArtifact.objects.filter(run=run).count() >= settings.ARTIFACTS_PER_RUN_MAX:
                raise ArtifactError(
                    f"This run already published {settings.ARTIFACTS_PER_RUN_MAX} artifacts, the maximum."
                )
            artifact.save()
    except BaseException:
        name = artifact.file.name or artifact.file.field.generate_filename(artifact, safe_name)
        delete_stored_file(artifact.file.storage, name)
        raise
    return artifact


async def areplace_artifact(
    run: Run, artifact_id: str, *, filename: str, content: bytes, title: str = ""
) -> RunArtifact:
    """Replace the file of artifact ``artifact_id`` of ``run``'s session, keeping its id and URL, and add ``run`` to its
    ``revised_by``.

    ``filename`` renames it and sets its content type; a blank ``title`` keeps the current one. Raises
    :class:`ArtifactError` on a rejected file or an artifact the session does not have.
    """
    safe_name = _checked_name(filename, content)
    return await sync_to_async(_replace_artifact)(run, artifact_id, safe_name, content, title)


def _replace_artifact(run: Run, artifact_id: str, safe_name: str, content: bytes, title: str) -> RunArtifact:
    from sessions.models import RunArtifact, artifact_upload_to

    artifact = _session_artifact(run.session_id, artifact_id)
    storage = artifact.file.storage
    name = artifact_upload_to(artifact, safe_name, revision=uuid.uuid4().hex)
    try:
        name = storage.save(name, ContentFile(content), max_length=artifact.file.field.max_length)
        with transaction.atomic():
            # Concurrent revisions serialise on the row, so each one deletes the file the one before it left.
            if (replaced := RunArtifact.objects.select_for_update().filter(pk=artifact.pk).first()) is None:
                raise ArtifactError(f"this session has no artifact '{artifact_id}'.")
            artifact.file = name
            artifact.title = (title.strip() or replaced.title)[:200]
            artifact.filename = safe_name[:255]
            artifact.content_type = guess_content_type(safe_name)
            artifact.size = len(content)
            artifact.updated_at = timezone.now()
            artifact.save(update_fields=["title", "filename", "content_type", "size", "file", "updated_at"])
            artifact.revised_by.add(run)
            transaction.on_commit(partial(delete_stored_file, storage, replaced.file.name))
    except BaseException:
        delete_stored_file(storage, name)
        raise
    return artifact


async def aread_artifact(thread_id: str, artifact_id: str) -> ArtifactFile:
    """The current file of artifact ``artifact_id`` of the session ``thread_id``; raises :class:`ArtifactError`."""
    return await sync_to_async(_read_artifact)(thread_id, artifact_id)


def _read_artifact(thread_id: str, artifact_id: str) -> ArtifactFile:
    artifact = _session_artifact(thread_id, artifact_id)
    content = _read_stored_file(artifact)
    if content is None and reload_artifact(artifact):
        content = _read_stored_file(artifact)
    if content is None:
        logger.error("Artifact %s: stored file %s is missing", artifact.pk, artifact.file.name)
        raise ArtifactError(
            f"the stored file of artifact '{artifact_id}' is missing. Write the file again and publish it with "
            f'`artifact_id="{artifact_id}"` to restore the artifact at the same URL.'
        )
    return ArtifactFile(filename=artifact.filename, content=content)


def _read_stored_file(artifact: RunArtifact) -> bytes | None:
    try:
        with artifact.file.open("rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def reload_artifact(artifact: RunArtifact) -> bool:
    """SYNC ONLY: reload ``artifact`` in place; whether a revision replaced its file since it was loaded.

    A revision deletes the file it replaces, so a reader that finds its file missing reloads and, on ``True``, retries.
    """
    loaded_name = artifact.file.name
    try:
        artifact.refresh_from_db()
    except ObjectDoesNotExist:
        return False
    return artifact.file.name != loaded_name


def _session_artifact(thread_id: str, artifact_id: str) -> RunArtifact:
    from sessions.models import RunArtifact

    try:
        pk = uuid.UUID(artifact_id)
    except ValueError:
        raise ArtifactError(
            f"'{artifact_id}' is not an artifact id; pass the `id` from the artifact's publish result."
        ) from None
    artifacts = RunArtifact.objects.filter(run__session_id=thread_id, pk=pk).select_related("run")
    if (artifact := artifacts.first()) is None:
        raise ArtifactError(f"this session has no artifact '{artifact_id}'.")
    return artifact


def delete_stored_file(storage: Storage, name: str) -> None:
    try:
        storage.delete(name)
    except Exception:
        logger.exception("Failed to delete artifact file %s", name)


def delete_artifact_files(storage: Storage, name: str, artifact_id: str) -> None:
    """Delete ``name`` and every other stored file of artifact ``artifact_id``, by its storage-name prefix: a revision
    that committed after a deletion loaded the row leaves the row's ``name`` stale."""
    directory = PurePosixPath(name).parent
    try:
        _, files = storage.listdir(str(directory))
    except FileNotFoundError:
        files = []
    except Exception:
        logger.exception("Failed to list artifact files in %s", directory)
        files = []
    for stored in {name, *(str(directory / file) for file in files if file.startswith(artifact_id))}:
        delete_stored_file(storage, stored)


def serialize_artifact(artifact: RunArtifact) -> ArtifactPayload:
    """SYNC ONLY: the absolute URLs read the current ``Site``."""
    from core.utils import build_absolute_url

    return ArtifactPayload(
        id=str(artifact.id),
        title=artifact.title,
        filename=artifact.filename,
        content_type=artifact.content_type,
        kind=artifact.kind,
        size=artifact.size,
        url=build_absolute_url(artifact.get_absolute_url()),
        download_url=build_absolute_url(artifact.get_download_url()),
    )


type PublishStatus = Literal["published", "updated"]


def published_tool_result(payload: ArtifactPayload, status: PublishStatus = "published") -> str:
    """The ``publish_artifact`` success result, which the transcript's artifact card parses."""
    return json.dumps({"status": status, **payload.model_dump()})


class RunArtifactStore:
    """The executor's ``ArtifactStore``: a published file goes to the run that :func:`bind_active_run` names."""

    @property
    def max_bytes(self) -> int:
        return settings.ARTIFACT_MAX_BYTES

    @property
    def per_run_max(self) -> int:
        return settings.ARTIFACTS_PER_RUN_MAX

    async def aaccepts(self, thread_id: str) -> bool:
        return await aresolve_active_run(thread_id) is not None

    async def astore(
        self, *, thread_id: str, filename: str, content: bytes, title: str = "", artifact_id: str | None = None
    ) -> str:
        if (run := await aresolve_active_run(thread_id)) is None:
            raise ArtifactError("this run has no session to attach artifacts to.")
        status: PublishStatus
        if artifact_id:
            artifact = await areplace_artifact(run, artifact_id, filename=filename, content=content, title=title)
            status = "updated"
        else:
            artifact = await astore_artifact(run, filename=filename, content=content, title=title)
            status = "published"
        logger.info(
            "publish_artifact: run=%s %s artifact %s as %s (%s, %d bytes)",
            run.pk,
            status,
            artifact.pk,
            artifact.filename,
            artifact.content_type,
            artifact.size,
        )
        return await _apublished_result(artifact, status)

    async def aread(self, *, thread_id: str, artifact_id: str) -> ArtifactFile:
        return await aread_artifact(thread_id, artifact_id)


async def _apublished_result(artifact: RunArtifact, status: PublishStatus) -> str:
    try:
        payload = await sync_to_async(serialize_artifact)(artifact)
    except Exception:
        logger.exception("publish_artifact: stored artifact %s but could not build its absolute URLs", artifact.pk)
        return json.dumps({
            "status": status,
            "id": str(artifact.pk),
            "url": artifact.get_absolute_url(),
            "warning": "Stored, but DAIV could not build absolute URLs; the URL is relative to the DAIV host.",
        })
    return published_tool_result(payload, status)


async def aserialize_run_artifacts(run: Run) -> list[ArtifactPayload]:
    """``serialize_artifact`` for every artifact ``run`` published or revised, oldest first."""
    from sessions.models import RunArtifact

    artifacts = RunArtifact.objects.filter(Q(run=run) | Q(revised_by=run)).distinct().select_related("run")
    return await sync_to_async(lambda: [serialize_artifact(artifact) for artifact in artifacts])()


async def aserialize_run_artifacts_for_status(run: Run) -> tuple[list[ArtifactPayload], str | None]:
    """Artifacts for a job-status response, which must still answer when the listing fails."""
    try:
        return await aserialize_run_artifacts(run), None
    except Exception:
        logger.exception("Failed to list artifacts for run=%s", run.pk)
        return [], "The run's artifacts could not be listed; retry the status request later."
