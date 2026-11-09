"""Media from tool results: the images and files a tool result carried, kept so the transcript can show them.

``RunToolMediaStore`` is the agent's ``automation.agent.tool_media.ToolMediaStore`` for executor runs: a media block
goes to the ``Run`` that :func:`sessions.artifacts.bind_active_run` names, within the ``sessions.conf`` limits. The
content type comes from an MCP server or ``mimetypes``, so only types a browser can't run script from are kept, and
the endpoint serves them under the artifact sandbox CSP.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.core.files.base import ContentFile
from django.db import transaction

from asgiref.sync import sync_to_async

from automation.agent.tool_media import FAILED, RUN_LIMIT, TOO_LARGE, UNSUPPORTED_TYPE, ToolMediaError
from sessions.artifacts import aresolve_active_run, delete_stored_file
from sessions.conf import settings

if TYPE_CHECKING:
    from sessions.models import Run, ToolResultMedia

logger = logging.getLogger("daiv.sessions")

_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


def stored_content_type(mime_type: str) -> str | None:
    """``mime_type`` normalised (no parameters, lower case) if it may be kept, else ``None``."""
    content_type = mime_type.split(";", 1)[0].strip().lower()
    if content_type in _IMAGE_TYPES or content_type == "application/pdf":
        return content_type
    major, _, minor = content_type.partition("/")
    return content_type if major in ("audio", "video") and minor else None


class RunToolMediaStore:
    """The executor's ``ToolMediaStore``: a media block goes to the run that ``bind_active_run`` names."""

    async def astore(self, *, thread_id: str, tool_call_id: str, ordinal: int, mime_type: str, content: bytes) -> str:
        if (run := await aresolve_active_run(thread_id)) is None:
            logger.warning("Tool media of call %s: no active run on thread %s", tool_call_id, thread_id)
            raise ToolMediaError(FAILED)
        if (content_type := stored_content_type(mime_type)) is None:
            raise ToolMediaError(UNSUPPORTED_TYPE)
        if len(content) > settings.TOOL_MEDIA_MAX_BYTES:
            raise ToolMediaError(TOO_LARGE)
        media = await sync_to_async(_store_media)(run, tool_call_id, ordinal, content_type, content)
        logger.info(
            "Tool media: run=%s kept %s #%d of call %s (%s, %d bytes)",
            run.pk,
            media.pk,
            ordinal,
            tool_call_id,
            content_type,
            media.size,
        )
        return str(media.pk)


def _store_media(run: Run, tool_call_id: str, ordinal: int, content_type: str, content: bytes) -> ToolResultMedia:
    from sessions.models import Run, ToolResultMedia

    media = ToolResultMedia(
        run=run, tool_call_id=tool_call_id, ordinal=ordinal, content_type=content_type, size=len(content)
    )
    try:
        media.file.save("media", ContentFile(content), save=False)
        with transaction.atomic():
            # Parallel tool calls in one model turn store concurrently; the lock serialises the budget check.
            Run.objects.select_for_update().only("pk").get(pk=run.pk)
            if ToolResultMedia.objects.filter(run=run).count() >= settings.TOOL_MEDIA_PER_RUN_MAX:
                raise ToolMediaError(RUN_LIMIT)
            media.save()
    except BaseException:
        name = media.file.name or media.file.field.generate_filename(media, "media")
        delete_stored_file(media.file.storage, name)
        raise
    return media
